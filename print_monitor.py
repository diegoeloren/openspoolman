from logger import log
import time
from print_history import insert_print, insert_filament_usage
from print_context import (
    PrintContext,
    STATE_IDLE,
    STATE_PRINTING,
    STATE_FINAL,
)

from config import (
    TRACK_LAYER_USAGE,
)

from filament_usage_tracker import FilamentUsageTracker
FILAMENT_TRACKER = FilamentUsageTracker()

# ------------------------------------------------------------
# PrintMonitor States (Application States)
# ------------------------------------------------------------
PMS_UNKNOWN =   "UNKNOWN"
PMS_IDLE =      "WAITING FOR JOB"
PMS_GATHERING = "GATHERING JOB META"
PMS_PREPARE =   "PREPARE TRACKING"
PMS_TRACKING =  "TRACKING MATERIAL"
PMS_DONE =      "DONE"


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
        Process incoming data messages by MQTT
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
            ctx.printer_state,
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
                f"(printer={ctx.printer_state})"
            )

        else:
            log(
                f"[PMS] {printer_id}: "
                f"{new_pms} "
                f"(printer={ctx.printer_state})"
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

            # OpenSpoolMan is started and printer is in IDLE
            if printer_state == STATE_IDLE:
                return PMS_IDLE

            # OpenSpoolMan is started and printer is in FINAL (A job was aborted or finished)
            if printer_state == STATE_FINAL:
                return PMS_IDLE
            
            # OpenSpoolMan started in the middle of a print
            if printer_state == STATE_PRINTING:
                # Not Handled at the moment
                pass

        # ----------------------------------------------------
        # IDLE
        # ----------------------------------------------------
        if old_pms == PMS_IDLE:
            # if OpenSpoolMan is waiting for the job and is
            # Job has been triggered (CLOUD, LAN, LOCAL)
            if ctx.is_triggered():
                return PMS_GATHERING

        # ----------------------------------------------------
        # GATHERING - Meta Dat From MQTT
        # ----------------------------------------------------
        if old_pms == PMS_GATHERING:

            if printer_state == STATE_PRINTING:
                if ctx.is_ready():
                    return PMS_PREPARE

        # ----------------------------------------------------
        # PREPARATION - Init Downloads, Parsing Files, Request Print ID
        # ----------------------------------------------------
        if old_pms == PMS_PREPARE:

            if ctx.is_ready_for_download():
            #if ctx.is_tracking():
                return PMS_TRACKING

        # ----------------------------------------------------
        # TRACKING
        # ----------------------------------------------------
        if old_pms == PMS_TRACKING:

            # if print job is finished (how to detect success/failure/abortion)
            if (printer_state == STATE_FINAL) or (printer_state == STATE_IDLE):
                return PMS_DONE

        # ----------------------------------------------------
        # DONE
        # ----------------------------------------------------
        if old_pms == PMS_DONE:
            return PMS_IDLE

        # If not changed, remain in 
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
        if new_pms == PMS_IDLE:
            #self.on_idle(ctx)
            pass

        elif new_pms == PMS_GATHERING:
            self.on_gathering_start(ctx)
        
        elif new_pms == PMS_PREPARE:
            self.on_prepare_start(ctx)

        elif new_pms == PMS_TRACKING:
            self.on_print_start(ctx)

        elif new_pms == PMS_DONE:
            self.on_print_done(ctx)

    # --------------------------------------------------------
    # EVENT HANDLERS
    # --------------------------------------------------------
    # Prepartion from printer side (automatically accumulated via MQTT)
    def on_gathering_start(self, ctx: PrintContext):
        log(
            f"[PMS EVENT] gathering started: "
            f"{ctx.job_label}"
        )

    # Prepartions needed from OpenSpoolMan Side
    def on_prepare_start(self, ctx: PrintContext):
        log(
            f"[PMS EVENT] prepare started: "
            f"{ctx.job_label}"
        )

        if not ctx.is_downloaded():
            ctx.download()      # Download and Parse 3mf-file
            if not ctx.is_tracking():
                new_print_id = insert_print(
                    ctx.get_task(),
                    ctx.get_source_type(),
                    ctx.get_metadata().get("image"),
                )

                # Insert filaments used.
                self.apply_filaments(ctx,new_print_id)

                # AMS Mapping
                act_mapping = ctx.get_mapping() # Incomplete for local jobs / external spool holder
                FILAMENT_TRACKER.apply_ams_mapping(act_mapping)

                # Handover meta
                meta = ctx.get_metadata()
                FILAMENT_TRACKER.set_print_metadata(meta)

                # Set the Tracking
                ctx.set_tracking(new_print_id)
        
    # While Printing
    def on_print_start(self, ctx: PrintContext):
        log(
            f"[PMS EVENT] running: "
            f"{ctx.info()}"
        )
        data = ctx.last_raw
        FILAMENT_TRACKER.on_message(data)

    # Printing finished
    def on_print_done(self, ctx: PrintContext):
        log(
            f"[PMS EVENT] print finished: "
            f"{ctx.job_label}"
        )
        ctx.reset()

    def apply_filaments(self,ctx,print_id):
        filaments = ctx.get_metadata().get("filaments",{}).items()
        for id, filament in filaments:
            parsed_grams = self._parse_floats(filament.get("used_g"))
            parsed_length_m = self._parse_floats(filament.get("used_m"))
            estimated_length_mm = parsed_length_m * 1000 if parsed_length_m is not None else None
            grams_used = parsed_grams if parsed_grams is not None else 0.0
            length_used = estimated_length_mm if estimated_length_mm is not None else 0.0
            if TRACK_LAYER_USAGE:
                grams_used = 0.0
                length_used = 0.0
            insert_filament_usage(
                print_id,
                filament["type"],
                filament["color"],
                grams_used,
                id,
                estimated_grams=parsed_grams,
                length_used=length_used,
                estimated_length=estimated_length_mm,
            )

    def _parse_floats(self,value):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
