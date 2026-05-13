from dataclasses import dataclass, field
from typing import Any
from copy import deepcopy
import time

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


@dataclass
class PrintContext:
    printer_id: str

    # Internally managed attributes (on Update)
    timestamp : float | None = None         # Automatically stamped on Readiness
    printer_state: str | None = None        # Streamed and aggregated through data
    source_type: str | None = None          # LOCAL, LAN, CLOUD
    job_label: str | None = None            # 
    task: str | None = None
    job_file: str | None = None

    # Externally set attributes, mainly by PrintMonitor
    print_id: int | None = None
    tracking_started: bool = False
    download_done: bool = False

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
        
        """
        ToDos
            source_type: str | None = None
            job_file: str | None = None
        """

        if self.timestamp is None and self.is_ready():
            self.timestamp = time.time()

    def reset(self) -> None:
        """
        Reset runtime-related attributes.
        """
        self.printer_state = None
        self.source_type = None

        self.job_label = None
        self.job_file = None
        self.task = None
        self.print_id = None
        self.download_done = False
        self.timestamp = None

        self.last_raw.clear()
        self.summary.clear()

    def is_triggered(self) -> bool:
        if self._detect_source() is None:
            return False
        return True

    def is_ready(self) -> bool:
        """
        Determines if the print context has sufficient information gathered.
        """
        # Depending on the source_type several information are necessary to reach readiness
        return ((self.source_type is not None) and          # Source is known
                (self.printer_state == STATE_PRINTING) and  # Printer_state is PRINTING
                (self.job_label is not None) and            # job_label is known
                (self.job_file is not None) and             # file is known
                (self.download_done is True)                # file is cached for layer tracking
                )
    
    def _detect_source(self) -> str:
        """
        Detection logic for different types of sources.
        Once the detection logic is set, it latches.
        It is intended only to be cleared on .reset() function to "ARM" it again
        """
        # if Source has been detected, it can only be set after reset
        if self.source_type is not None:
            return self.source_type
        
        # Determine if the source is of type 'LAN' or 'Cloud'
        command = self.summary.get("command")
        target  = self.summary.get("url")
        if command  == "project_file" and target:
            if not target.startswith("http"):
                self.source_type = JOB_TYPE_LAN
            else:
                self.source_type = JOB_TYPE_CLOUD

            self.job_label = self.get_job_label()
            self.task = self.get_task()
            return
        
        # Determine if the source is of type 'local'
        print_type = (self.summary.get("print_type") or "").upper()
        if self.printer_state == STATE_PRINTING and print_type == JOB_TYPE_LOCAL:
            self.source_type = JOB_TYPE_LOCAL
            self.job_label = self.get_job_label()
            self.task = self.get_task()
            return
        

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