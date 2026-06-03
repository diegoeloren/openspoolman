from logger import log
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
        self.contexts: dict[str, PrintContext] = {}
        self.state: dict[str, str] = {}

    # --------------------------------------------------------
    # MAIN ENTRY
    # --------------------------------------------------------
    def process(self, printer_id: str, data: dict):

        if "print" not in data:
            return

        ignored_commands = {
            "extrusion_cali_get",
            "extrusion_cali_set",
        }

        block = data.get("print", {})
        if block.get("command") in ignored_commands:
            return

        ctx = self._ctx(printer_id)

        old_pms = self._get_state(printer_id)

        ctx.update(block)

        new_pms = self._transition_pms(
            old_pms,
            ctx.printer_state,
            ctx,
        )

        self._set_state(printer_id, new_pms)

        if new_pms != old_pms:
            self._handle_transition(
                ctx,
                old_pms,
                new_pms,
                data,
            )

            log(
                f"[PMS] {printer_id}: {old_pms} -> {new_pms} "
                f"(printer={ctx.printer_state})"
            )

        # --------------------------------------------------------
        # FILAMENT TRACKER RUNTIME
        # --------------------------------------------------------

        if FILAMENT_TRACKER.active_model is not None:
            FILAMENT_TRACKER.handle_runtime_message(ctx.last_raw)

        if new_pms == PMS_TRACKING:
            layer = ctx.last_raw.get("layer_num")
            if layer is not None:
                FILAMENT_TRACKER.handle_layer_change(layer)

    # --------------------------------------------------------
    # CONTEXT HANDLING
    # --------------------------------------------------------
    def _ctx(self, printer_id: str) -> PrintContext:
        if printer_id not in self.contexts:
            self.contexts[printer_id] = PrintContext(printer_id=printer_id)
        return self.contexts[printer_id]

    # --------------------------------------------------------
    # STATE STORAGE
    # --------------------------------------------------------
    def _get_state(self, printer_id: str) -> str:
        return self.state.setdefault(printer_id, PMS_UNKNOWN)

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

        if old_pms == PMS_UNKNOWN:

            if printer_state == STATE_IDLE:
                return PMS_IDLE

            if printer_state == STATE_FINAL:
                return PMS_IDLE

            if printer_state == STATE_PRINTING:
                return PMS_TRACKING

        if old_pms == PMS_IDLE:
            if ctx.is_triggered():
                return PMS_GATHERING

        if old_pms == PMS_GATHERING:
            if ctx.is_ready():
                return PMS_PREPARE

        if old_pms == PMS_PREPARE:
            if ctx.is_ready_for_download():
                return PMS_TRACKING

        if old_pms == PMS_TRACKING:
            if printer_state in (STATE_FINAL, STATE_IDLE):
                return PMS_DONE

        if old_pms == PMS_DONE:
            return PMS_IDLE

        return old_pms

    # --------------------------------------------------------
    # TRANSITION HANDLER
    # --------------------------------------------------------
    def _handle_transition(
        self,
        ctx: PrintContext,
        old_pms: str,
        new_pms: str,
        data: dict,
    ):

        if new_pms == PMS_GATHERING:
            self.on_gathering_start(ctx)

        elif new_pms == PMS_PREPARE:
            self.on_prepare_start(ctx)

        elif new_pms == PMS_TRACKING:
            if old_pms == PMS_UNKNOWN:
                self.on_restore_tracking(ctx)
            else:
                self.on_print_started(ctx)

        elif new_pms == PMS_DONE:
            self.on_print_done(ctx)

    # --------------------------------------------------------
    # EVENTS
    # --------------------------------------------------------
    def on_gathering_start(self, ctx: PrintContext):
        log(f"[PMS EVENT] gathering started: {ctx.job_label}")

    def on_prepare_start(self, ctx: PrintContext):
        log(f"[PMS EVENT] prepare started: {ctx.job_label}")

        if not ctx.is_downloaded():
            ctx.download()

        if ctx.is_tracking():
            return

        image = ctx.get_metadata().get("image")

        new_print_id = insert_print(
            ctx.get_task(),
            ctx.get_source_type(),
            image,
        )

        self.apply_filaments(ctx, new_print_id)

        ctx.set_tracking(new_print_id)

    def on_print_started(self, ctx: PrintContext):
        log(f"[PMS EVENT] print started: {ctx.info()}")

        meta = ctx.get_metadata()

        started = FILAMENT_TRACKER.start_print(
            print_metadata=meta,
            model_path=meta.get("downloaded_model_path"),
            gcode_file_name=meta.get("gcode_path"),
            use_ams=ctx.get_ams_usage(),
            ams_mapping=ctx.get_mapping(),
            task_id=ctx.get_task_id(),
            subtask_id=ctx.get_subtask_id(),
        )

        if not started:
            log("[PMS EVENT] filament tracker failed to initialize")

    def on_restore_tracking(self, ctx: PrintContext):
        log("[PMS EVENT] attempting checkpoint recovery")

        recovered = FILAMENT_TRACKER._attempt_print_resume(
            ctx.get_task_id(),
            ctx.get_subtask_id(),
        )

        if recovered:
            log("[PMS EVENT] checkpoint restored")
            return
        log(
            "[PMS EVENT] recovery failed, "
            "tracker will remain inactive"
        )

    def on_print_done(self, ctx: PrintContext):
        log(f"[PMS EVENT] print finished: {ctx.job_label}")
        ctx.reset()

    # --------------------------------------------------------
    # FILAMENT DB INSERT
    # --------------------------------------------------------
    def apply_filaments(self, ctx, print_id):

        filaments = ctx.get_metadata().get("filaments", {}).items()

        for fid, filament in filaments:

            parsed_grams = self._parse_floats(filament.get("used_g"))
            parsed_length_m = self._parse_floats(filament.get("used_m"))

            length_mm = parsed_length_m * 1000 if parsed_length_m else 0.0
            grams = parsed_grams or 0.0

            if TRACK_LAYER_USAGE:
                grams = 0.0
                length_mm = 0.0

            insert_filament_usage(
                print_id,
                filament["type"],
                filament["color"],
                grams,
                fid,
                estimated_grams=parsed_grams,
                length_used=length_mm,
                estimated_length=parsed_length_m * 1000 if parsed_length_m else None,
            )

            log(
                f"[Print-Context] filament id={fid} "
                f"linked to print_id={print_id}"
            )

    def _parse_floats(self, value):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None