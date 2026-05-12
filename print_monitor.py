from logger import append_to_rotating_file, log
import time
from print_context import (
    PrintContext,
    STATE_IDLE,
    STATE_PRINTING,
    STATE_FINAL,
)

# ------------------------------------------------------------
# Raw printer state mappings from bambu_state.py
# ------------------------------------------------------------

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

# ------------------------------------------------------------
# PrintMonitor States (Application States)
# ------------------------------------------------------------

PMS_UNKNOWN = "UNKNOWN"
PMS_IDLE = "WAITING FOR JOB"
PMS_GATHERING = "GATHERING JOB META"
PMS_TRACKING = "TRACKING MATERIAL"
PMS_PAUSED = "PAUSED"
PMS_ABORTING = "ABORTING"
PMS_DONE = "DONE"


class PrintMonitor:

    def __init__(self):

        # printer_id -> PrintContext
        self.contexts: dict[str, PrintContext] = {}

        # printer_id -> PMS state
        self.state: dict[str, str] = {}

    # --------------------------------------------------------
    # MAIN ENTRY
    # --------------------------------------------------------
    def process(self, printer_id: str, data: dict):
        """
        Process incoming data messages
        """

        # Guard #1: Skips messages whichout "print" key word
        if "print" not in data:
            return

        # Guard #2: Slicer frequently requests extrusion_cal settings, these message are skipped
        ignored_commands = {
            "extrusion_cali_get",
            "extrusion_cali_set",
        }
        block = data.get("print", {})
        command = block.get("command")
        if command in ignored_commands:
            return

        # Beginn of Message processing
        # get or create context for the printer_id
        ctx = self._ctx(printer_id)

        # Store the current PMS state for that printer
        old_pms = self._get_state(printer_id)

        # enrich context from incoming printer data
        ctx.update(block)

        # determine next PMS state
        new_pms = self._transition_pms(
            old_pms,
            ctx.state,
            ctx,
        )

        # store PMS state
        self._set_state(printer_id, new_pms)

        # transition hook
        if new_pms != old_pms:
            self._handle_transition(
                ctx,
                old_pms,
                new_pms,
                data,
            )

            log(
                f"[PMS] {printer_id}: "
                f"{old_pms} -> {new_pms} "
                f"(printer={ctx.state})"
            )

        else:
            log(
                f"[PMS] {printer_id}: "
                f"{new_pms} "
                f"(printer={ctx.state})"
            )

    # --------------------------------------------------------
    # Catch Context; Generate one if not present 
    # --------------------------------------------------------
    def _ctx(self, printer_id: str) -> PrintContext:

        if printer_id not in self.contexts:
            self.contexts[printer_id] = PrintContext(
                printer_id=printer_id
            )

        return self.contexts[printer_id]

    # --------------------------------------------------------
    # PMS STATE STORAGE
    # --------------------------------------------------------
    def _get_state(self, printer_id: str) -> str:
        return self.state.setdefault(
            printer_id,
            PMS_UNKNOWN,
        )

    def _set_state(self, printer_id: str, state: str):
        self.state[printer_id] = state

    # --------------------------------------------------------
    # SOURCE DETECTION
    # --------------------------------------------------------
    def _detect_source_type(self, block: dict) -> str:

        print_type = (
            block.get("print_type") or ""
        ).lower()

        # printer started directly on device
        if print_type == "local":
            return "LOCAL"

        url = block.get("url") or ""

        # LAN mode often has ftp://
        if isinstance(url, str):
            if url.startswith("ftp://"):
                return "LAN"

        return "CLOUD"

    # --------------------------------------------------------
    # PMS TRANSITIONS
    # --------------------------------------------------------

    def _transition_pms(
        self,
        old_pms: str,
        printer_state: str,
        ctx: PrintContext,
    ) -> str:

        # ----------------------------------------------------
        # UNKNOWN
        # ----------------------------------------------------

        if old_pms == PMS_UNKNOWN:

            if printer_state == STATE_IDLE:
                return PMS_IDLE

            if printer_state == STATE_PREPARING:
                return PMS_GATHERING

            if printer_state == STATE_FINAL:
                return PMS_IDLE

        # ----------------------------------------------------
        # IDLE
        # ----------------------------------------------------

        if old_pms == PMS_IDLE:

            if printer_state == STATE_PREPARING:
                return PMS_GATHERING

            if printer_state == STATE_PRINTING:
                return PMS_TRACKING

        # ----------------------------------------------------
        # GATHERING
        # ----------------------------------------------------

        if old_pms == PMS_GATHERING:

            if printer_state == STATE_PRINTING:
                return PMS_TRACKING

            if printer_state == STATE_FINAL:
                return PMS_ABORTING

            if printer_state == STATE_IDLE:
                return PMS_ABORTING

        # ----------------------------------------------------
        # TRACKING
        # ----------------------------------------------------

        if old_pms == PMS_TRACKING:

            if printer_state == STATE_FINAL:
                return PMS_DONE

            if printer_state == STATE_IDLE:
                return PMS_ABORTING

        # ----------------------------------------------------
        # ABORTING
        # ----------------------------------------------------

        if old_pms == PMS_ABORTING:

            if printer_state == STATE_IDLE:
                return PMS_IDLE

        # ----------------------------------------------------
        # DONE
        # ----------------------------------------------------

        if old_pms == PMS_DONE:

            if printer_state == STATE_IDLE:
                return PMS_IDLE

        # stay where we are
        return old_pms

    # --------------------------------------------------------
    # TRANSITION HOOKS
    # --------------------------------------------------------

    def _handle_transition(
        self,
        ctx: PrintContext,
        old_pms: str,
        new_pms: str,
        data: dict,
    ):

        # ----------------------------------------------------
        # example hooks
        # ----------------------------------------------------

        if new_pms == PMS_GATHERING:
            self.on_prepare_start(ctx)

        elif new_pms == PMS_TRACKING:
            self.on_print_start(ctx)

        elif new_pms == PMS_DONE:
            self.on_print_done(ctx)

        elif new_pms == PMS_ABORTING:
            self.on_print_abort(ctx)

    # --------------------------------------------------------
    # EVENT HANDLERS
    # --------------------------------------------------------

    def on_prepare_start(self, ctx: PrintContext):

        log(
            f"[EVENT] prepare started: "
            f"{ctx.job_label}"
        )

    def on_print_start(self, ctx: PrintContext):

        log(
            f"[EVENT] print started: "
            f"{ctx.job_label}"
        )

    def on_print_done(self, ctx: PrintContext):

        log(
            f"[EVENT] print finished: "
            f"{ctx.job_label}"
        )

    def on_print_abort(self, ctx: PrintContext):

        log(
            f"[EVENT] print aborted: "
            f"{ctx.job_label}"
        )