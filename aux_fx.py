"""
This file covers auxilary function used accross the project
"""

from typing import Any
from datetime import datetime
from config import TZINFO


def now():
    return datetime.now(TZINFO)

MODEL_CODE_TO_NAME = {
  # H2 series
  "093": "H2S",
  "094": "H2D",
  "239": "H2D Pro",
  "109": "H2C",
  # X1 series
  "00W": "X1",
  "00M": "X1 Carbon",
  "03W": "X1E",
  # P1 series
  "01S": "P1P",
  "01P": "P1S",
  # P2 series
  "22E": "P2S",
  # A1 series
  "039": "A1",
  "030": "A1 Mini",
}

def get_printer_model_name(printer_id: str | None) -> str:
  if not printer_id:
    return "Unknown"
  model_code = printer_id[:3]
  return MODEL_CODE_TO_NAME.get(model_code, f"Unknown model ({model_code})")

def get_print_block(payload: dict) -> dict:
  if not payload:
    return {}
  return payload.get("print") or {}


def extract_gcode_state(payload: dict) -> str | None:
  return get_print_block(payload).get("gcode_state")


def extract_print_status(payload: dict) -> str | None:
  return get_print_block(payload).get("print_status")


def extract_prepare_percent(payload: dict) -> int | None:
  return get_print_block(payload).get("gcode_file_prepare_percent")


def normalize_prepare_percent(value: Any) -> int | None:
  if value is None:
    return None
  if isinstance(value, str):
    if not value.strip():
      return None
  try:
    numeric = float(value)
  except (TypeError, ValueError):
    return None
  percent = int(numeric)
  if percent < 0:
    percent = 0
  if percent > 100:
    percent = 100
  return percent


def is_print_active(gcode_state: str | None, print_status: str | None) -> bool:
  if gcode_state and gcode_state.upper() in PRINTING_GCODE_STATES:
    return True
  if print_status and print_status.upper() in PRINTING_STATUS_STATES:
    return True
  return False

def is_print_final(gcode_state: str | None, print_status: str | None) -> bool:
  if gcode_state and gcode_state.upper() in FINAL_GCODE_STATES:
    return True
  if print_status and print_status.upper() in FINAL_STATUS_STATES:
    return True
  return False

def normalize_extra_value(value):
  """
  Normalize legacy Spoolman extra values.

  Old versions stored values double-json-encoded:
      "\"abc\""

  This function converts:
      "\"abc\"" -> "abc"

  It also safely handles None and non-string values.
  """

  if value is None:
    return ""

  if not isinstance(value, str):
    return str(value)

  value = value.strip()

  #
  # Legacy double-stringified JSON
  #
  try:
    parsed = json.loads(value)

    if isinstance(parsed, str):
      return parsed.strip()

  except Exception:
    pass

  return value.strip().strip('"')  