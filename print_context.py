from dataclasses import dataclass, field

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
    printer_state: str | None = None
    source_type: str | None = None
    job_label: str | None = None
    job_file: str | None = None

    # Externally set attributes, mainly by PrintMonitor
    print_id: int | None = None
    tracking_started: bool = False
    early_download_started: bool = False

    # Contains the raw content of the latest received message
    last_raw: dict[str, Any] = field(default_factory=dict)

    # Contains merged information across multiple messages
    summary: dict[str, Any] = field(default_factory=dict)

    def update(self, new_data: dict[str, Any]) -> None:
        """
        Store the latest raw message and merge it into the summary.
        """
        self.last_raw = new_data.deepcopy()
        self.summary.update(new_data)

        # Fill all states
        self.printer_state = self._derive_printer_state() # Update printer_state
        
        """
        ToDos
            source_type: str | None = None
            job_label: str | None = None
            job_file: str | None = None
        """

        if self.timestamp is None and self.isReady():
            self.timestamp = time.time()

    def reset(self) -> None:
        """
        Reset runtime-related attributes.
        """
        self.printer_state = None
        self.source_type = None

        self.job_label = None
        self.job_file = None
        self.print_id = None

        self.tracking_started = False
        self.download_done = False

        self.timestamp = None

        self.last_raw.clear()
        self.summary.clear()

    def isReady(self) -> bool:
        """
        Determines if the print context is has sufficient information gathered.
        """
        # Depending on the source_type several information are necessary to reach readiness
        return ((self.source_type is not None) and          # Source is known
                (self.printer_state == STATE_PRINTING) and  # Printer_state is PRINTING
                (self.job_label is not None) and            # job_label is known
                (self.job_file is not None) and             # file is known
                (self.download_done is True)                # file is cached for layer tracking
                )

    def _derive_printer_state(self) -> str:

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