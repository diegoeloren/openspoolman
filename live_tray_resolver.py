"""
live_tray_resolver.py

Resolves filament_index → tray_id live from MQTT signals,
without relying on job metadata (ams_mapping, use_ams).

Works for all job sources: LAN, CLOUD, LOCAL.

The resolver observes three AMS fields per MQTT push_status:
  - ams_status   (bitmask, see AMS_STATUS_* constants)
  - tray_now     (currently active tray, 255 = transitioning)
  - tray_pre     (previously active tray)

And one extruder field:
  - extruder.info[0].star  (target slot announced before ams_status changes)

Wechsel signature (from recordings):
  768 (stable) → star changes         ← earliest signal, tray_tar also set
                → ams_status → 258    ← swap start
                → tray_now  → 255     ← ejecting old filament
                → tray_now  → target  ← new filament loaded
                → ams_status → 263    ← purging / flushing
                → ams_status → 768    ← SETTLED (confirmed)
                   AND tray_pre == tray_now

Binding rule:
  filament_index → tray_now  is recorded exactly once,
  at the SETTLED moment (ams_stable AND tray_pre == tray_now).

Startup / warmup:
  The first SETTLED event after print start binds filament index 0
  (or whatever the GCode-first filament is).  The sequence of
  SETTLED events across the print builds the complete mapping.

External spool (tray_now == 255 at stable time):
  Not possible by definition — 255 is the transitioning sentinel.
  External spool is indicated by tray_now == EXTERNAL_TRAY_ID (254).
"""

from logger import log

# ── AMS status bitmask constants (derived from recordings) ──────────────────
#
#  Bits 9+8 = main mode:
#    0b00 = 0x000 =   0  →  AMS off / no job
#    0b11 = 0x300 = 768  →  STABLE (printing or idle with spool loaded)
#
#  Bit 8 set, Bit 9 clear = swap in progress (0x100..0x1FF):
#    0x102 = 258  swap start, ejecting
#    0x103 = 259  ejecting phase 1
#    0x104 = 260  ejecting phase 2
#    0x105 = 261  tray_now=255, loading starts
#    0x106 = 262  loading in progress
#    0x107 = 263  purging / flushing (tray_now already at target)
#
_AMS_STABLE_MASK     = 0x300   # bits 9+8 both set
_AMS_SWAPPING_BIT    = 0x100   # bit 8 set, bit 9 clear → swap running
_AMS_TRAY_EXTERNAL   = 254     # bambu external spool tray id
_AMS_TRAY_SENTINEL   = 255     # "in transit" sentinel value


def _ams_is_stable(ams_status: int | None) -> bool:
    """True when AMS is printing/idle with a confirmed spool loaded."""
    if ams_status is None:
        return False
    return (ams_status & _AMS_STABLE_MASK) == _AMS_STABLE_MASK


def _ams_is_swapping(ams_status: int | None) -> bool:
    """True during any phase of a filament swap."""
    if ams_status is None:
        return False
    return (ams_status & _AMS_SWAPPING_BIT) != 0 and \
           (ams_status & _AMS_STABLE_MASK) != _AMS_STABLE_MASK


def _tray_int(value) -> int | None:
    """Convert tray_now/tray_pre/tray_tar string/int to int, or None."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class LiveTrayResolver:
    """
    Observes MQTT push_status messages and resolves
    filament_index → physical tray_id on-the-fly.

    Usage:
        resolver = LiveTrayResolver()
        resolver.start_print()

        # per MQTT message:
        resolver.update(print_block)          # feed the raw print-dict
        tray_id = resolver.resolve(filament_index)
        # None → usage should be buffered until resolved
    """

    def __init__(self):
        self._index_to_tray: dict[int, int] = {}
        self._pending_filament_index: int | None = None
        self._prev_ams_status: int | None = None
        self._prev_tray_now: int | None = None
        self._prev_star: int | None = None
        self._swap_count: int = 0
        self._gcode_filament_sequence: list[int] = []
        self._active = False
        self._on_settled_callback = None  # callable(filament_index, tray_id) | None

    # ── public API ──────────────────────────────────────────────────────────

    def start_print(
        self,
        gcode_filament_sequence: list[int] | None = None,
        on_settled=None,
    ) -> None:
        """
        Reset resolver state for a new print.

        gcode_filament_sequence: ordered list of filament indices as they
        appear in the GCode (from evaluate_gcode M620 order).
        Example: [0, 1, 0, 2]  means the GCode uses filament 0 first,
        then switches to 1, back to 0, then 2.

        on_settled: optional callable(filament_index: int, tray_id: int)
        Called immediately when a binding is confirmed (SETTLED).
        Use this to flush pending usage right away instead of waiting
        for the next layer commit.
        """
        self._index_to_tray = {}
        self._pending_filament_index = None
        self._prev_ams_status = None
        self._prev_tray_now = None
        self._prev_star = None
        self._swap_count = 0
        self._gcode_filament_sequence = list(gcode_filament_sequence or [])
        self._on_settled_callback = on_settled
        self._active = True
        log("[LiveTrayResolver] started, gcode_sequence="
            f"{self._gcode_filament_sequence}")

    def stop_print(self) -> None:
        """Called when print ends or aborts."""
        self._active = False
        log(f"[LiveTrayResolver] stopped, final map={self._index_to_tray}")

    def update(self, print_block: dict) -> None:
        """
        Feed a MQTT print-block (the value of payload['print']).
        Call this on every push_status message while tracking.
        """
        if not self._active:
            return

        ams_block   = print_block.get("ams") or {}
        ams_status  = print_block.get("ams_status")
        tray_now    = _tray_int(ams_block.get("tray_now"))
        tray_pre    = _tray_int(ams_block.get("tray_pre"))

        ext_info    = (print_block.get("device") or {}) \
                          .get("extruder", {}) \
                          .get("info", [{}])
        star        = (ext_info[0] if ext_info else {}).get("star")

        self._process(ams_status, tray_now, tray_pre, star)

        self._prev_ams_status = ams_status
        self._prev_tray_now   = tray_now
        self._prev_star       = star

    def resolve(self, filament_index: int) -> int | None:
        """
        Return the physical tray_id for a filament_index, or None
        if the binding is not yet confirmed (caller should buffer usage).
        """
        return self._index_to_tray.get(filament_index)

    def has_ams(self) -> bool | None:
        """
        Returns True if at least one AMS tray (not external) has been
        confirmed, False if only external tray seen, None if unknown yet.
        """
        if not self._index_to_tray:
            return None
        for tray_id in self._index_to_tray.values():
            if tray_id != _AMS_TRAY_EXTERNAL:
                return True
        return False

    def get_mapping(self) -> dict[int, int]:
        """Return a copy of the current filament_index → tray_id map."""
        return dict(self._index_to_tray)

    # ── internal ────────────────────────────────────────────────────────────

    def _process(
        self,
        ams_status: int | None,
        tray_now: int | None,
        tray_pre: int | None,
        star: int | None,
    ) -> None:

        # ── Detect swap announcement: star changes while AMS is stable ──────
        # This is the earliest signal, appears before ams_status changes.
        if (
            _ams_is_stable(self._prev_ams_status)
            and star is not None
            and star != _AMS_TRAY_SENTINEL  # 255 = no target / unloaded
            and star != self._prev_star
            and self._prev_star is not None
        ):
            pending = self._next_pending_filament_index()
            if pending is not None:
                log(f"[LiveTrayResolver] swap announced: "
                    f"star {self._prev_star}→{star}, "
                    f"pending filament_index={pending}")
                self._pending_filament_index = pending

        # ── Detect SETTLED: ams_status stable AND tray_pre == tray_now ──────
        if (
            _ams_is_stable(ams_status)
            and tray_now is not None
            and tray_now != _AMS_TRAY_SENTINEL
            and tray_pre is not None
            and tray_pre == tray_now
            and not _ams_is_stable(self._prev_ams_status)  # came from swap
        ):
            self._on_settled(tray_now)

    def _on_settled(self, tray_now: int) -> None:
        """
        A filament swap has completed and tray_now is confirmed stable.
        Bind the pending filament_index to this tray.

        Binding rules:
          _swap_count == 0 AND _pending_filament_index is None:
            First startup load — bind the first GCode filament index.
            This happens exactly once per print.

          _pending_filament_index is not None:
            A swap was announced (star changed) — bind that index.

          All other SETTLED events (no announcement, not startup):
            Ignore. These occur during leveling, purging, and other
            printer routines that touch the AMS without a real swap.
        """
        if self._pending_filament_index is None:
            if self._swap_count > 0:
                # Not a real swap — leveling/purge/other routine.
                log(f"[LiveTrayResolver] SETTLED tray={tray_now} ignored "
                    "(no swap announced, not startup)")
                return
            # swap_count == 0: first startup load — bind first GCode filament.
            filament_index = self._next_pending_filament_index()
            if filament_index is None:
                log(f"[LiveTrayResolver] SETTLED tray={tray_now} "
                    "but no pending filament index (sequence exhausted?)")
                return
        else:
            filament_index = self._pending_filament_index
            self._pending_filament_index = None

        self._swap_count += 1
        old = self._index_to_tray.get(filament_index)

        if old is not None and old != tray_now:
            log(f"[LiveTrayResolver] WARNING: filament {filament_index} "
                f"previously mapped to tray {old}, now re-binding to {tray_now}")

        self._index_to_tray[filament_index] = tray_now

        log(f"[LiveTrayResolver] SETTLED #{self._swap_count}: "
            f"filament_index={filament_index} → tray={tray_now} "
            f"(map={self._index_to_tray})")

        if self._on_settled_callback is not None:
            try:
                self._on_settled_callback(filament_index, tray_now)
            except Exception as exc:
                log(f"[LiveTrayResolver] on_settled callback error: {exc}")

    def _next_pending_filament_index(self) -> int | None:
        """
        Returns the next unbound filament index from the GCode sequence,
        or None if the sequence is exhausted or unavailable.
        """
        for idx in self._gcode_filament_sequence:
            if idx not in self._index_to_tray:
                return idx

        # Sequence exhausted or not provided — fall back to lowest unbound
        # integer that could logically follow (best-effort).
        bound = set(self._index_to_tray.keys())
        for candidate in range(16):   # max 16 AMS slots ever
            if candidate not in bound:
                return candidate
        return None
