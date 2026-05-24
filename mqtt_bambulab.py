

import json
import os
import ssl
import traceback
from pathlib import Path
from threading import Thread
from typing import Any, Iterable
from urllib.parse import unquote

import paho.mqtt.client as mqtt

from config import (
    PRINTER_ID,
    PRINTER_CODE,
    PRINTER_IP,
    AUTO_SPEND,
    EXTERNAL_SPOOL_ID,
    TRACK_LAYER_USAGE,
    CLEAR_ASSIGNMENT_WHEN_EMPTY,
)
from messages import GET_VERSION, PUSH_ALL, AMS_FILAMENT_SETTING
from spoolman_service import spendFilaments, setActiveTray, fetchSpools, clear_active_spool_for_tray
from tools_3mf import getMetaDataFrom3mf
import time
import copy
from collections.abc import Mapping
from logger import append_to_rotating_file, log
from print_history import insert_print, insert_filament_usage
from filament_usage_tracker import FilamentUsageTracker
from aux_fx import (
  get_printer_model_name,
)
from print_monitor import (
  PrintMonitor,
)
PRINT_MONITOR = PrintMonitor()


MQTT_CLIENT = {}  # Global variable storing MQTT Client
MQTT_CLIENT_CONNECTED = False
MQTT_KEEPALIVE = 60
LAST_AMS_CONFIG = {}  # Global variable storing last AMS configuration

PRINTER_STATE = {}
PRINTER_STATE_LAST = {}

PENDING_PRINT_METADATA = {}
LAST_LAN_PROJECT = {}
LOG_FILE = os.getenv("OPENSPOOLMAN_MQTT_LOG_PATH", "/home/app/logs/mqtt.log")
_LOG_WRITE_FAILED = False
PENDING_PRINT_REFERENCE = {}
PRINTER_MODEL_NAME = get_printer_model_name(PRINTER_ID)
LAST_PREPARE_LOGGED = object()

def _build_model_cache_path(printer_id: str) -> Path:
  safe_printer_id = "".join(
    char if char.isalnum() or char in ("-", "_") else "_"
    for char in str(printer_id or "unknown")
  )
  return Path(__file__).resolve().parent / "data" / "cache" / f"{safe_printer_id}.3mf"

MODEL_CACHE_PATH = _build_model_cache_path(PRINTER_ID)

def getPrinterModel():
    global PRINTER_ID
    model_code = PRINTER_ID[:3]

    model_map = {
      # H2-Serie
      "093": "H2S",
      "094": "H2D",
      "239": "H2D Pro",
      "109": "H2C",

      # X1-Serie
      "00W": "X1",
      "00M": "X1 Carbon",
      "03W": "X1E",

      # P1-Serie
      "01S": "P1P",
      "01P": "P1S",

      # P2-Serie
      "22E": "P2S",

      # A1-Serie
      "039": "A1",
      "030": "A1 Mini"
    }

    model_name = model_map.get(model_code, f"Unknown model ({model_code})")

    numeric_tail = ''.join(filter(str.isdigit, PRINTER_ID))
    device_id = numeric_tail[-3:] if len(numeric_tail) >= 3 else numeric_tail

    device_name = f"3DP-{model_code}-{device_id}"

    return {
        "model": model_name,
        "devicename": device_name
    }

def identify_ams_model_from_module(module: dict[str, Any]) -> str | None:
    """Guess the AMS variant that a version module represents."""

    product_name = (module.get("product_name") or "").strip().lower()
    module_name = (module.get("name") or "").strip().lower()

    if "ams lite" in product_name or module_name.startswith("ams_f1"):
        return "AMS Lite"
    if "ams 2 pro" in product_name or module_name.startswith("n3f"):
        return "AMS 2 Pro"
    if "ams ht" in product_name or module_name.startswith("ams_ht"):
        return "AMS HT"
    if module_name == "ams" or module_name.startswith("ams/"):
        return "AMS"

    return None


def identify_ams_models_from_modules(modules: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
  """Return per-module metadata, including the detected model when available."""

  results: dict[str, dict[str, Any]] = {}
  for module in modules or []:
    name = module.get("name")
    if not name:
      continue

    results[name] = {
      "model": identify_ams_model_from_module(module),
      "product_name": module.get("product_name"),
      "serial": module.get("sn"),
      "hw_ver": module.get("hw_ver"),
    }

  return results


def extract_ams_id_from_module_name(name: str) -> int | None:
  parts = name.split("/")
  if len(parts) != 2:
    return None
  try:
    return int(parts[1])
  except ValueError:
    return None


def identify_ams_models_by_id(modules: Iterable[dict[str, Any]]) -> dict[str, str]:
  """Return the detected AMS model per numeric AMS ID (module suffix)."""

  results: dict[str, str] = {}
  for module in modules or []:
    name = module.get("name")
    if not name:
      continue

    ams_id = extract_ams_id_from_module_name(name)
    if ams_id is None:
      continue

    model = identify_ams_model_from_module(module)
    if model:
      results[str(ams_id)] = model
      results[ams_id] = model

  return results


def num2letter(num):
  return chr(ord("A") + int(num))
  
def update_dict(original: dict, updates: dict) -> dict:
    for key, value in updates.items():
        if isinstance(value, Mapping) and key in original and isinstance(original[key], Mapping):
            original[key] = update_dict(original[key], value)
        else:
            original[key] = value
    return original


def _parse_grams(value):
  try:
    return float(value)
  except (TypeError, ValueError):
    return None

def _is_lan_project_url(url: str | None) -> bool:
  if not url:
    return False
  return not url.startswith("http")

def _is_valid_print_id(value: Any) -> bool:
  if value is None:
    return False
  if isinstance(value, str) and not value.strip():
    return False
  try:
    return int(value) != 0
  except (TypeError, ValueError):
    return True

def _normalize_print_file_label(label: Any) -> str | None:
  if not isinstance(label, str):
    return None
  normalized = unquote(label.strip())
  if not normalized:
    return None
  normalized = normalized.replace("\\", "/").rsplit("/", 1)[-1]
  lowered = normalized.lower()
  for suffix in (".gcode.3mf", ".3mf", ".gcode"):
    if lowered.endswith(suffix):
      normalized = normalized[: -len(suffix)]
      break
  normalized = normalized.strip().lower()
  return normalized or None

def _matches_print_identity(print_state: dict, identity: dict) -> bool:
  if not print_state or not identity:
    return False
  if _is_valid_print_id(identity.get("task_id")) and _is_valid_print_id(print_state.get("task_id")):
    if str(identity.get("task_id")) == str(print_state.get("task_id")):
      return True
  if _is_valid_print_id(identity.get("subtask_id")) and _is_valid_print_id(print_state.get("subtask_id")):
    if str(identity.get("subtask_id")) == str(print_state.get("subtask_id")):
      return True
  identity_file = identity.get("file") or identity.get("gcode_file") or identity.get("subtask_name")
  state_file = print_state.get("gcode_file") or print_state.get("subtask_name")
  normalized_identity_file = _normalize_print_file_label(identity_file)
  normalized_state_file = _normalize_print_file_label(state_file)
  if normalized_identity_file and normalized_state_file:
    return normalized_identity_file == normalized_state_file
  return bool(identity_file and state_file and identity_file == state_file)

def _lan_project_is_active(identity: dict) -> bool:
  if not identity:
    return False
  timestamp = identity.get("timestamp")
  if not timestamp:
    return False
  return (time.time() - timestamp) < (6 * 60 * 60)

def _mask_serial(serial: str | None, keep_chars: int = 3) -> str:
  if not serial:
    return ""
  visible = serial[:keep_chars]
  if len(serial) <= keep_chars:
    return visible
  return f"{visible}..."

def _mask_sn_values(value):
  if isinstance(value, dict):
    for key, item in value.items():
      if key.lower() == "sn" and isinstance(item, str):
        value[key] = _mask_serial(item)
      else:
        _mask_sn_values(item)
  elif isinstance(value, list):
    for elem in value:
      _mask_sn_values(elem)

def _mask_mqtt_payload(payload: str) -> str:
  try:
    data = json.loads(payload)
    _mask_sn_values(data)
    masked = json.dumps(data, separators=(",", ":"))
  except ValueError:
    masked = payload

  masked_serial = _mask_serial(PRINTER_ID)
  if masked_serial:
    masked = masked.replace(PRINTER_ID, masked_serial)

  return masked


def iter_mqtt_payloads_from_lines(lines: Iterable[str]) -> Iterable[dict[str, Any]]:
  """Yield decoded MQTT payloads from log lines (format: '<timestamp> :: <json>')."""
  for line in lines:
    if "::" not in line:
      continue
    payload_raw = line.split("::", 1)[1].strip()
    if not payload_raw:
      continue
    try:
      payload = json.loads(payload_raw)
    except (TypeError, ValueError):
      continue
    if isinstance(payload, dict):
      yield payload


def iter_mqtt_payloads_from_log(log_path: str | Path) -> Iterable[dict[str, Any]]:
  """Yield decoded MQTT payloads from a saved mqtt.log file."""
  path = Path(log_path)
  with path.open("r", encoding="utf-8") as handle:
    yield from iter_mqtt_payloads_from_lines(handle)


def replay_mqtt_payloads(payloads: Iterable[dict[str, Any]]) -> None:
  """Replay decoded MQTT payloads through the on_message handler."""
  for payload in payloads:
    msg = type("ReplayMsg", (), {"payload": json.dumps(payload).encode("utf-8")})()
    on_message(None, None, msg)


def replay_mqtt_log(log_path: str | Path) -> None:
  """Replay an mqtt.log file through the on_message handler."""
  replay_mqtt_payloads(iter_mqtt_payloads_from_log(log_path))

def _cleanup_cached_download(metadata: dict | None, skip_path: str | None = None) -> None:
  if not metadata:
    return
  cached_model_path = metadata.get("downloaded_model_path")
  if not cached_model_path:
    return
  if skip_path and cached_model_path == skip_path:
    return
  try:
    os.remove(cached_model_path)
  except FileNotFoundError:
    return
  except OSError as exc:
    log(f"[WARNING] Failed to remove cached model file {cached_model_path!r}: {exc}")

def _set_pending_print_metadata(metadata: dict | None) -> None:
  global PENDING_PRINT_METADATA
  if metadata is PENDING_PRINT_METADATA:
    return
  next_cached_path = metadata.get("downloaded_model_path") if isinstance(metadata, dict) else None
  _cleanup_cached_download(PENDING_PRINT_METADATA, skip_path=next_cached_path)
  PENDING_PRINT_METADATA = metadata or {}

def _cleanup_startup_model_cache() -> None:
  try:
    os.remove(MODEL_CACHE_PATH)
    log(f"[DEBUG] Removed stale startup model cache: {MODEL_CACHE_PATH}")
  except FileNotFoundError:
    return
  except OSError as exc:
    log(f"[WARNING] Failed to remove startup model cache {str(MODEL_CACHE_PATH)!r}: {exc}")


_cleanup_startup_model_cache()

def _maybe_download_metadata(source: str | None, reason: str) -> dict | None:
  if not source:
    log(f"[DEBUG] Metadata download skipped: no source ({reason}).")
    return None
  try:
    metadata = getMetaDataFrom3mf(
      source,
      keep_downloaded_file=TRACK_LAYER_USAGE,
      downloaded_file_path=str(MODEL_CACHE_PATH) if TRACK_LAYER_USAGE else None,
    )
    if metadata:
      log(f"[DEBUG] Metadata downloaded for {reason} from {source}")
    return metadata
  except TypeError as exc:
    # Compatibility fallback for tests/patches that still stub an older signature.
    if "downloaded_file_path" in str(exc):
      metadata = getMetaDataFrom3mf(source, keep_downloaded_file=TRACK_LAYER_USAGE)
    elif "keep_downloaded_file" in str(exc):
      metadata = getMetaDataFrom3mf(source)
    else:
      raise
    if metadata:
      log(f"[DEBUG] Metadata downloaded for {reason} from {source}")
    return metadata
  except Exception as exc:
    log(f"[WARNING] Metadata download failed ({reason}): {exc}")
    return None

def map_filament(tray_tar):
  global PENDING_PRINT_METADATA
  # Prüfen, ob ein Filamentwechsel aktiv ist (stg_cur == 4)
  #if stg_cur == 4 and tray_tar is not None:
  if PENDING_PRINT_METADATA:
    if not PENDING_PRINT_METADATA.get("use_ams", True):
      return False

    filament_changes = PENDING_PRINT_METADATA.setdefault("filamentChanges", [])
    filament_changes.append(tray_tar)  # Jeder Wechsel zählt, auch auf das gleiche Tray
    log(f'Filamentchange {len(filament_changes)}: Tray {tray_tar}')

    # Anzahl der erkannten Wechsel
    change_count = len(filament_changes) - 1  # -1, weil der erste Eintrag kein Wechsel ist

    filament_order = PENDING_PRINT_METADATA.get("filamentOrder") or {}
    ordered_filaments = sorted(filament_order.items(), key=lambda entry: entry[1])
    assigned_trays = PENDING_PRINT_METADATA.setdefault("assigned_trays", [])
    filament_assigned = None
    if tray_tar not in assigned_trays:
      assigned_trays.append(tray_tar)
      unique_index = len(assigned_trays) - 1
      if unique_index < len(ordered_filaments):
        filament_assigned = ordered_filaments[unique_index][0]
      else:
        for filamentId, usage_count in filament_order.items():
          if usage_count == change_count:
            filament_assigned = filamentId
            break

    if filament_assigned is not None:
      mapping = PENDING_PRINT_METADATA.setdefault("ams_mapping", [])
      try:
        filament_idx = int(filament_assigned)
      except (TypeError, ValueError):
        filament_idx = None
      if filament_idx is None:
        return False
      mapping_index = filament_idx
      while len(mapping) <= mapping_index:
        mapping.append(None)
      mapping[filament_idx] = tray_tar
      log(f"✅ Tray {tray_tar} assigned to Filament {filament_assigned}")

      for filament, tray in enumerate(mapping):
        if tray is None:
          continue
        log(f"  Filament pos: {filament} → Tray {tray}")

    target_filaments_raw = set(filament_order.keys())
    if target_filaments_raw and all(str(key).isdigit() for key in target_filaments_raw):
      target_filaments = {int(key) for key in target_filaments_raw}
    else:
      target_filaments = target_filaments_raw
    if target_filaments:
      assigned_filaments = {
        idx for idx, tray in enumerate(PENDING_PRINT_METADATA.get("ams_mapping", []))
        if tray is not None
      }
      if target_filaments.issubset(assigned_filaments):
        log("\n✅ All trays assigned:")
        return True
  
  return False

def publish(client, msg):
  result = client.publish(f"device/{PRINTER_ID}/request", json.dumps(msg))
  status = result[0]
  if status == 0:
    log(f"Sent {msg} to topic device/{PRINTER_ID}/request")
    return True

  log(f"Failed to send message to topic device/{PRINTER_ID}/request")
  return False


def clear_ams_tray_assignment(ams_id, tray_id):
  if not MQTT_CLIENT:
    return

  ams_message = copy.deepcopy(AMS_FILAMENT_SETTING)
  ams_message["print"]["ams_id"] = int(ams_id)
  ams_message["print"]["tray_id"] = int(tray_id)
  ams_message["print"]["slot_id"] = int(int(tray_id) % 4)
  ams_message["print"]["tray_color"] = ""
  ams_message["print"]["nozzle_temp_min"] = None
  ams_message["print"]["nozzle_temp_max"] = None
  ams_message["print"]["tray_type"] = ""
  ams_message["print"]["setting_id"] = ""
  ams_message["print"]["tray_info_idx"] = ""

  publish(MQTT_CLIENT, ams_message)

# Inspired by https://github.com/Donkie/Spoolman/issues/217#issuecomment-2303022970
def on_message(client, userdata, msg):
  global LAST_AMS_CONFIG, PRINTER_STATE, PRINTER_STATE_LAST, PENDING_PRINT_METADATA, PRINTER_MODEL
  
  try:
    data = json.loads(msg.payload.decode())

    info = data.get("info")
    if info and info.get("command") == "get_version":
      modules = info.get("module", [])
      detected = identify_ams_models_from_modules(modules)
      models_by_id = identify_ams_models_by_id(modules)
      LAST_AMS_CONFIG["get_version"] = {
        "info": info,
        "modules": modules,
        "detected_models": detected,
        "models_by_id": models_by_id,
      }

    if "print" in data:
      global _LOG_WRITE_FAILED
      if LOG_FILE and not _LOG_WRITE_FAILED:
        try:
          append_to_rotating_file(LOG_FILE, _mask_mqtt_payload(msg.payload.decode()))
        except OSError as exc:
          _LOG_WRITE_FAILED = True
          log(f"[WARNING] Failed to write MQTT log to {LOG_FILE!r}: {exc}")

    #print(data)

    if AUTO_SPEND:
        PRINT_MONITOR.process(PRINTER_ID, data)
      
    # Save external spool tray data
    if "print" in data and "vt_tray" in data["print"]:
      LAST_AMS_CONFIG["vt_tray"] = data["print"]["vt_tray"]

    # Save ams spool data
    if "print" in data and "ams" in data["print"] and "ams" in data["print"]["ams"]:
      LAST_AMS_CONFIG["ams"] = data["print"]["ams"]["ams"]
      for ams in data["print"]["ams"]["ams"]:
        log(f"AMS [{num2letter(ams['id'])}] (hum: {ams['humidity']}, temp: {ams['temp']}ºC)")
        for tray in ams["tray"]:
          if "tray_sub_brands" in tray:
            #log(f"    - [{num2letter(ams['id'])}{tray['id']}] {tray['tray_sub_brands']} {tray['tray_color']} ({str(tray['remain']).zfill(3)}%) [[ {tray['tray_uuid']} ]]")

            found = False
            tray_uuid = "00000000000000000000000000000000"

            for spool in fetchSpools(True):

              tray_uuid = tray["tray_uuid"]

              if not spool.get("extra", {}).get("tag"):
                continue
              tag = json.loads(spool["extra"]["tag"])
              if tag != tray["tray_uuid"]:
                continue

              found = True

              setActiveTray(spool['id'], spool["extra"], ams['id'], tray["id"])

              # TODO: filament remaining - Doesn't work for AMS Lite
              # requests.patch(f"http://{SPOOLMAN_IP}:7912/api/v1/spool/{spool['id']}", json={
              #  "remaining_weight": tray["remain"] / 100 * tray["tray_weight"]
              # })

            if not found and tray_uuid == "00000000000000000000000000000000":
              return
              log("      - non Bambulab Spool!")
            elif not found:
              #log("      - Not found. Update spool tag!")
              tray["unmapped_bambu_tag"] = tray_uuid
              tray["issue"] = True
              clear_active_spool_for_tray(ams['id'], tray['id'])
              clear_ams_tray_assignment(ams['id'], tray['id'])
          else:
            return
            log(f"    - [{num2letter(ams['id'])}{tray['id']}]")
            log("      - No Spool!")

  except Exception:
    traceback.print_exc()

def on_connect(client, userdata, flags, rc):
  global MQTT_CLIENT_CONNECTED
  MQTT_CLIENT_CONNECTED = True
  log("Connected with result code " + str(rc))
  client.subscribe(f"device/{PRINTER_ID}/report")
  publish(client, GET_VERSION)
  publish(client, PUSH_ALL)

def on_disconnect(client, userdata, rc):
  global MQTT_CLIENT_CONNECTED
  MQTT_CLIENT_CONNECTED = False
  log("Disconnected with result code " + str(rc))
  
def async_subscribe():
  global MQTT_CLIENT
  global MQTT_CLIENT_CONNECTED
  
  MQTT_CLIENT_CONNECTED = False
  MQTT_CLIENT = mqtt.Client()
  MQTT_CLIENT.username_pw_set("bblp", PRINTER_CODE)
  ssl_ctx = ssl.create_default_context()
  ssl_ctx.check_hostname = False
  ssl_ctx.verify_mode = ssl.CERT_NONE
  MQTT_CLIENT.tls_set_context(ssl_ctx)
  MQTT_CLIENT.tls_insecure_set(True)
  MQTT_CLIENT.on_connect = on_connect
  MQTT_CLIENT.on_disconnect = on_disconnect
  MQTT_CLIENT.on_message = on_message
  
  while True:
    while not MQTT_CLIENT_CONNECTED:
      try:
          log("🔄 Trying to connect ...", flush=True)
          MQTT_CLIENT.connect(PRINTER_IP, 8883, MQTT_KEEPALIVE)
          MQTT_CLIENT.loop_start()
          
      except Exception as exc:
          log(f"⚠️ connection failed: {exc}, new try in 15 seconds...", flush=True)

      time.sleep(15)

    time.sleep(15)

def init_mqtt(daemon: bool = False):
  # Start the asynchronous processing in a separate thread
  thread = Thread(target=async_subscribe, daemon=daemon)
  thread.start()

def getLastAMSConfig():
  global LAST_AMS_CONFIG
  return LAST_AMS_CONFIG


def getDetectedAmsModelsById():
  global LAST_AMS_CONFIG
  detected = LAST_AMS_CONFIG.get("get_version", {}).get("models_by_id") or {}
  return dict(detected)


def getMqttClient():
  global MQTT_CLIENT
  return MQTT_CLIENT

def isMqttClientConnected():
  global MQTT_CLIENT_CONNECTED

  return MQTT_CLIENT_CONNECTED
