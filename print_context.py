from dataclasses import dataclass, field
from typing import Any
from logger import log
from copy import deepcopy
from aux_fx import now


# Debugging only #
import json
from pathlib import Path

LOG_DIR = Path("/home/app/logs/")
LOG_DIR.mkdir(exist_ok=True)

from config import (
    PRINTER_ID,
    PRINTER_CODE,
    PRINTER_IP,
    AUTO_SPEND,
    EXTERNAL_SPOOL_ID,
    TRACK_LAYER_USAGE,
    CLEAR_ASSIGNMENT_WHEN_EMPTY,
)

def _build_model_cache_path(printer_id: str) -> Path:
  safe_printer_id = "".join(
    char if char.isalnum() or char in ("-", "_") else "_"
    for char in str(printer_id or "unknown")
  )
  return Path(__file__).resolve().parent / "data" / "cache" / f"{safe_printer_id}.3mf"

MODEL_CACHE_PATH = _build_model_cache_path(PRINTER_ID)
from tools_3mf import getMetaDataFrom3mf



def dump_state(name, data):
    ts = now().strftime("%Y%m%d_%H%M%S")

    base_path = LOG_DIR / f"{name}_{ts}"
    path = base_path.with_suffix(".json")

    counter = 1

    while path.exists():
        path = base_path.with_name(f"{base_path.name}_{counter}").with_suffix(".json")
        counter += 1

    with open(path, "w", encoding="utf-8") as f:
        json.dump(
            data,
            f,
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        )

    return path
# Debugging only end #

PRINTING_GCODE_STATES = {"RUNNING", "PAUSE", "PAUSED"}
FINAL_GCODE_STATES = {
  "FINISH",
  "FAILED",
  "STOP",
  "STOPPED",
  "CANCEL",
  "CANCELLED",
  "CANCELED",
  "ABORT",
  "ABORTED",
  "ERROR",
  "IDLE",
}

PRINTING_STATUS_STATES = {"RUNNING", "PAUSE", "PAUSED"}
FINAL_STATUS_STATES = {
  "FINISH",
  "FAILED",
  "STOP",
  "STOPPED",
  "CANCEL",
  "CANCELLED",
  "CANCELED",
  "ABORT",
  "ABORTED",
  "ERROR",
  "IDLE",
}

# Printer States
STATE_IDLE =        "IDLE"
STATE_PRINTING =    "PRINTING"
STATE_FINAL =       "FINAL"

# Job Types
JOB_TYPE_LOCAL   = "LOCAL"
JOB_TYPE_LAN     = "LAN"
JOB_TYPE_CLOUD   = "CLOUD"

# To Do - AMS Mapping Resolver - LAN/CLOUD is command based (seed) - for LOCAL choose AMS Mapping Strategy
# Connection to existing code

@dataclass
class PrintContext:
    printer_id: str

    # Internally managed attributes (on Update)
    timestamp : float | None = None         # Automatically stamped on Readiness
    printer_state: str | None = None        # Streamed and aggregated through data
    source_type: str | None = None          # LOCAL, LAN, CLOUD
    job_label: str | None = None            # 
    task: str | None = None

    # Externally set attributes, mainly by PrintMonitor
    print_id: int | None = None
    download_done: bool = False
    tracking_started: bool = False
    # cached model required

    # Contains the raw content of the latest received message
    metadata: dict[str, Any] = field(default_factory=dict)

    # Contains the raw content of the latest received message
    last_raw: dict[str, Any] = field(default_factory=dict)

    # Contains merged information across multiple messages
    summary: dict[str, Any] = field(default_factory=dict)

    def update(self, new_data: dict[str, Any]) -> None:
        """
        Store the latest raw message and merge it into the summary.
        """
        self.last_raw = deepcopy(new_data)
        self.summary.update(self.last_raw)

        # Fill all states
        self.printer_state = self._derive_printer_state()   # Update printer_state
        self._detect_source()                               # Detection logic also sets
    
        if self.timestamp is None and self.is_ready():
            self.timestamp = now()

    def reset(self) -> None:
        """
        Reset runtime-related attributes.
        """
        self.printer_state = None
        self.source_type = None

        self.job_label = None
        self.task = None
        self.print_id = None
        self.download_done = False
        self.tracking_started: False
        self.timestamp = None

        self.metadata.clear()
        self.last_raw.clear()
        self.summary.clear()

    def is_triggered(self) -> bool:
        if (self.source_type is None):
            return False
        return True

    def is_ready_for_download(self) -> bool:
        """
        Determines if the download/caching of the 3mf- print can be started.
        """
        if not self.is_ready():
            return False
        return self.is_prepared()

    def is_tracking(self) -> bool:
        """
        Shows if a valid print_id has been assigned for tracking
        """
        if not self.is_downloaded():
            return False
        if self.print_id is None:
            return False
        return True

    def set_tracking(self, tracking_id: int) -> None:
        """
        Assignes the id from the filamenttracker to the context, it is ready for tracking
        """
        if self.is_downloaded():
            self.tracking_started = True
            self.print_id = tracking_id

    def is_ready(self) -> bool:
        """
        Determines if the print context has sufficient information collect sufficient information
        about the job. Are enough information present to start download?
        """
        # Depending on the source_type several information are necessary to reach readiness

        source_type = self.source_type
        if source_type is None:
            return False

        ans = False
        if source_type in (JOB_TYPE_LAN, JOB_TYPE_CLOUD):
            ans = ((self.printer_state == STATE_PRINTING) and  # Printer_state is PRINTING
                (self.job_label is not None) and            # job_label is known
                (self.task is not None)                     # file is known
                )

        # handle LAN based
        if source_type in (JOB_TYPE_LOCAL):
            task = self.get_task()
            if task is not None:
                self.task = task
                ans = ((self.printer_state == STATE_PRINTING) and
                    (self.job_label is not None))          # job_label is known

        log(f"[DEBUG] Readiness: Source: {self.source_type} JobLabel={self.job_label} task={self.task}")
    
        return ans
    
    def _detect_source(self) -> None:
        """
        Detection logic for different types of sources.
        Once the detection logic is set, it latches.
        It is intended only to be cleared on .reset() function to "ARM" it again
        """

        # if Source has been detected, it can only be set after reset
        if self.source_type is not None:
            return

        # Determine if the source is of type 'LAN' or 'Cloud' or 'Local'
        command = self.summary.get("command")
        target = self.summary.get("url")
        source = None

        if command == "project_file" and target:

            if target.startswith(("http://", "https://")):
                source = JOB_TYPE_CLOUD

            # Maybe X1C specific
            elif target.startswith("file:///sdcard") or target.startswith("file:///userdata"):
                source = JOB_TYPE_LOCAL

            # Orca used file:// and bambu (2.7beta) used ftp
            elif target.startswith("file://") or target.startswith("ftp://"):
                source = JOB_TYPE_LAN

        if source is not None:
            self.job_label = self.get_job_label()
            self.task = self.get_task()
            self.source_type = source
        
    def _derive_printer_state(self) -> str:
        """
        Aggregates the printers state
        """
        gcode = self.summary.get("gcode_state") or None
        status = self.summary.get("print_status") or None

        g = (gcode or "").upper()
        s = (status or "").upper()

        # FINAL
        if (
            g in FINAL_GCODE_STATES
            or s in FINAL_STATUS_STATES
        ):
            return STATE_FINAL

        # ACTIVE / PRINTING
        if (
            g in PRINTING_GCODE_STATES
            or s in PRINTING_STATUS_STATES
        ):
            return STATE_PRINTING
        # fallback
        return STATE_IDLE

    def is_prepared(self) -> bool:
        """
        Checks the readiness of a print.
        """
        value = self.summary.get("gcode_file_prepare_percent")

        if value is None:
            return False

        if isinstance(value, str) and not value.strip():
            return False

        try:
            percent = float(value)
        except (TypeError, ValueError):
            return False

        return percent >= 100
    
    def get_job_label(self) -> str | None:
        """
        Extracts the job label
        """
        url = self.summary.get("url")
        if isinstance(url, str) and url.strip():
            return url
        gcode_file = self.summary.get("gcode_file")
        if isinstance(gcode_file, str) and gcode_file.strip():
            return gcode_file
        return None
    
    def get_task(self) -> str | None:
        """
        Extracts the task name
        """
        task = self.summary.get("subtask_name")
        if task:
            return task
        return None

    def get_task_id(self) -> int:
        """
        Extracts the task id
        """
        task = self.summary.get("task_id")
        if task:
            return task
        return 0

    def get_subtask_id(self) -> int:
        """
        Extracts the subtask id
        """
        task = self.summary.get("subtask_id")
        if task:
            return task
        return 0

    def get_source_type(self) -> str | None:
        return self.source_type

    def get_metadata(self) -> dict:
        """
        Returns the metadata which are required for the filamenttracker.
        It is set manually.
        """
        self.metadata["print_id"] = self.get_printid()
        self.metadata["use_ams"] = self.get_ams_usage()
        self.metadata["print_type"] = self.get_source_type
        self.metadata["task_id"] = self.summary.get("task_id")
        self.metadata["subtask_id"] = self.summary.get("subtask_id")
        return self.metadata

    def get_printid(self) -> dict:
        return self.print_id

    def get_summary(self) -> dict:
        return self.summary

    def get_ams_usage(self) -> bool:
        use_ams_raw = self.summary.get("use_ams",False)
        use_ams = None
        if use_ams_raw is None:
            use_ams = history_print_type == "local"
        elif isinstance(use_ams_raw, str):
            use_ams = use_ams_raw.strip().lower() in ("1", "true", "yes", "on")
        else:
            use_ams = bool(use_ams_raw)
        return use_ams

    def info(self) -> str:
        """
        Concatenates all basic attributes to one string
        """
        sum = f"timestamp: {self.timestamp} | printer_id: {self.printer_id} | printer_state: {self.printer_state} | source_type: {self.source_type} | job_label: {self.job_label} | task: {self.task} | print_id: {self.print_id} | tracking_started: {self.tracking_started} | download_done: {self.download_done}"
        return sum

    def is_downloaded(self):
        return self.download_done

    def download(self):
        """
        Downloads the 3mf model and parses it.
        Captures the meta data into the meta dict.
        """
        source = self.summary.get("url")
        if not source:
            log(f"[DEBUG] Metadata download skipped: no source (project-file).")
            self.metadata = None
            self.download_done = False
            return
        try:
            self.metadata = getMetaDataFrom3mf(
            source,
            keep_downloaded_file=TRACK_LAYER_USAGE,
            downloaded_file_path=str(MODEL_CACHE_PATH) if TRACK_LAYER_USAGE else None,
            )
            if self.metadata:
                log(f"[DEBUG] Metadata downloaded for project-file from {source}")
                pass
            self.download_done = True
            return
        except TypeError as exc:
            # Compatibility fallback for tests/patches that still stub an older signature.
            if "downloaded_file_path" in str(exc):
                self.metadata = getMetaDataFrom3mf(source, keep_downloaded_file=TRACK_LAYER_USAGE)
            elif "keep_downloaded_file" in str(exc):
                self.metadata = getMetaDataFrom3mf(source)
            else:
                raise
            self.download_done = True
            return 
        except Exception as exc:
            log(f"[WARNING] Metadata download failed ({reason}): {exc}")
            self.metadata = None
            self.download_done = False
            return

    def get_mapping(self):
        return self.summary.get("ams_mapping") or []
        