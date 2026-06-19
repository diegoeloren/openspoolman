# <img alt="logo" src="static/logo.png" height="36" /> OpenSpoolMan — diegoeloren fork

> **Forked from [drndos/openspoolman](https://github.com/drndos/openspoolman)**
> This fork targets reliable operation on a **Bambu Lab X1C in LAN-only / Developer Mode** without any cloud dependency.

---

## What this fork changes vs. the original

### 🆕 Live MQTT-based AMS tray resolver (`LiveTrayResolver`)

The original openspoolman relies on `ams_mapping` metadata from the `project_file` MQTT command to assign filaments to trays. This only works for **cloud-initiated prints** — LAN and local prints don't carry this metadata.

This fork replaces the static mapping approach with a **live MQTT signal observer** (`live_tray_resolver.py`) that watches AMS state transitions in real time:

- Detects filament swap announcements via `extruder.star` changes
- Confirms tray bindings at **SETTLED** state (`ams_status & 0x300 == 0x300` AND `tray_pre == tray_now`)
- Works for **all job sources**: LAN, LOCAL (sdcard), LOCAL (repeat-print / "Erneut drucken"), CLOUD
- Guards against spurious SETTLED events during bed leveling and purging

### 🆕 Multi-buildplate support with correct per-plate filament filtering

The original code collected filaments from **all plates** in `slice_info.config`, leading to phantom filament entries in print history for plates not being printed.

This fork:
- Reads `plate_idx` from MQTT (`push_status`) to identify the active plate
- Passes `plate_idx` through to `getMetaDataFrom3mf()` which now filters filaments per plate
- Reads `filament_sequence.json` from the 3MF archive as the authoritative swap sequence source (filament IDs, not AMS slot indices)
- Fixes incorrect startup tray binding when the printer switches filament immediately at job start (e.g. Plate 2 starting with a different filament than what was loaded from Plate 1)

### 🆕 "Erneut drucken" / repeat-print support

When a print is started via the printer's "Print again" button, the MQTT URL is `file:///userdata/project_file.gcode.3mf` — a generic placeholder that cannot be fetched via FTP. This fork:

- Detects this case via `"project_file.gcode" in url`
- Falls back to the cached 3MF from the last LAN/LOCAL print (`data/cache/<printer_id>.3mf`)
- Other LOCAL jobs (`file:///sdcard/...`) with real filenames continue to be fetched via FTP as normal

### 🆕 `print_id` persisted in checkpoint

On resume after a restart, the original code lost `print_id` — all DB writes after recovery went nowhere. This fork stores `print_id` in the checkpoint metadata and restores it on resume.

### 🆕 Filament DB inserts moved from `PrintMonitor` into `FilamentUsageTracker`

Previously `PrintMonitor.apply_filaments()` inserted filament rows at PREPARE time using all filaments from all plates. Now:

- `PrintMonitor` only calls `insert_print()` and `set_tracking()` — pure job management
- `FilamentUsageTracker.start_print()` receives the plate-filtered filament dict and calls `_apply_filaments()` internally
- `print_history` is a pure DB interface with no logic

### 🐛 Bug fixes vs. original

| Bug | Fix |
|-----|-----|
| Crash on repeat-print: `zipfile.ZipFile(None)` | `_load_model()` guards against `model_path=None`; `download()` sets `download_done = bool(metadata)` instead of unconditional `True` |
| `get_metadata()` crash when `metadata is None` | `if self.metadata is None: self.metadata = {}` guard added |
| `NameError: name 'reason'` in `download()` exception handler | Removed undefined variable |
| All filaments from all plates inserted into print history | Per-plate filtering via `plate_idx` from MQTT |
| Wrong filament→tray binding when Plate 2 starts with a filament not in Plate 2 | Startup SETTLED skipped when `star` already points to a different tray |
| `print_id = None` after checkpoint recovery | `print_id` now stored in and restored from checkpoint |
| M620 slot indices used as filament indices in resolver (off-by-one for multi-plate) | `filament_sequence.json` used as swap sequence source (filament IDs directly) |

### 🧹 Removed relics

The following were made obsolete by the `LiveTrayResolver` and have been removed:

- `PrintContext.get_ams_usage()` and `metadata["use_ams"]`
- `PrintContext.get_mapping()` (`ams_mapping` passthrough)
- `FilamentUsageTracker.apply_ams_mapping()` (was already a no-op)
- `FilamentUsageTracker._retrieve_model()` (replaced by `_load_model()`)
- `import tempfile`, `from urllib.parse import urlparse`, `download3mfFromCloud/FTP/LocalFilesystem` imports in tracker
- `existing.pop("ams_mapping", None)` in checkpoint

---

## Scope of this fork

Reliable filament tracking on a **Bambu Lab X1C in LAN-only / Developer Mode**. Cloud prints are not a priority. The focus is:

- Correct spool-to-print-job assignment across all job initiation modes
- Accurate per-layer filament consumption tracking
- Robust handling of multi-plate jobs
- No dependency on Bambu cloud services

---

## News (upstream)

- [v0.3.0](https://github.com/drndos/openspoolman/releases/tag/v0.3.0) - 23.12.2025 — more accurate filament accounting and layer tracking, higher-fidelity print history, and better Bambu Lab / AMS integration
- [v0.2.0](https://github.com/drndos/openspoolman/releases/tag/v0.2.0) - 07.12.2025 — Adds material-aware tray/spool mismatch detection, tray color cues, print reassign/pagination, spool material filters, and SpoolMan URL handling with refreshed responsive layouts.
- [v0.1.9](https://github.com/drndos/openspoolman/releases/tag/v0.1.9) - 25.05.2025 — Ships post-print spool assignment, multi-platform Docker images, customizable spool sorting, timezone config, and compatibility/uI polish.
- [v0.1.8](https://github.com/drndos/openspoolman/releases/tag/v0.1.8) - 20.04.2025 — Starts importing each filament's SpoolMan `filament_id` for accurate matching (requires the `filament_id` custom field).
- [v0.1.7](https://github.com/drndos/openspoolman/releases/tag/v0.1.7) - 17.04.2025 — Introduces print cost tracking, printer header info, SPA gating improvements, and fixes for drawer colors/local prints.
- [0.1.6](https://github.com/drndos/openspoolman/releases/tag/0.1.6) - 09.04.2025 — Published container images (main service + Helm chart) and packaged artifacts for easier deployments.

---

## Main features

#### Dashboard overview
*Overview over the trays and the assigned spools and spool information*
<img alt="OpenSpoolMan overview" src="docs/img/desktop_home.PNG" />

<details>
<summary>Desktop screenshots (expand to view)</summary>

<h4>Dashboard overview</h4>
<p>Overview over the trays and the assigned spools and spool information</p>
<img alt="Desktop dashboard" src="docs/img/desktop_home.PNG" />

<h4>Fill tray workflow</h4>
<p>Assign a spool to a tray with quick filters.</p>
<img alt="Desktop fill tray" src="docs/img/desktop_fill_tray.PNG" />

<h4>Print history</h4>
<p>Track every print with filament usage, used spools and costs.</p>
<img alt="Desktop print history" src="docs/img/desktop_print_history.PNG" />

<h4>Spool detail info</h4>
<p>Shows informations about the spool and allows to assign it to a tray.</p>
<img alt="Desktop spool info" src="docs/img/desktop_spool_info.jpeg" />

<h4>NFC tag assignment</h4>
<p>Assign and refresh NFC tags so you can scan them with you mobile and get directly to the spool info.</p>
<img alt="Desktop assign NFC" src="docs/img/desktop_assign_nfc.jpeg" />

<h4>Spool change view from print history</h4>
<p>Change or remove the spool assignment after a print Useful when the wrong spool was assigned or the print was canceled.</p>
<img alt="Desktop change spool" src="docs/img/desktop_change_spool.PNG" />

</details>

<details>
<summary>Mobile screenshots (expand to view)</summary>

<table>
  <tr>
    <td valign="top">
      <h4>Dashboard overview</h4>
      <p>Overview over the trays and the assigned spools and spool information</p>
      <img alt="Mobile dashboard" src="docs/img/mobile_home.PNG" />
    </td>
    <td valign="top">
      <h4>Fill tray workflow</h4>
      <p>Assign a spool to a tray with quick filters.</p>
      <img alt="Mobile fill tray" src="docs/img/mobile_fill_tray.PNG" />
    </td>
  </tr>
  <tr>
    <td valign="top">
      <h4>Print history</h4>
      <p>View recent prints, AMS slots, and filament usage anytime.</p>
      <img alt="Mobile print history" src="docs/img/mobile_print_history.PNG" />
    </td>
    <td valign="top">
      <h4>Spool detail info</h4>
      <p>Spool metadata and NFC tags are accessible on the phone.</p>
      <img alt="Mobile spool info" src="docs/img/mobile_spool_info.jpeg" />
    </td>
  </tr>
  <tr>
    <td valign="top">
      <h4>NFC tag assignment</h4>
      <p>Assign and refresh NFC tags so you can scan them with you mobile and get directly to the spool info.</p>
      <img alt="Mobile assign NFC" src="docs/img/mobile_assign_nfc.jpeg" />
    </td>
    <td valign="top">
      <h4>Spool change view from print history</h4>
      <p>Change or remove the spool assignment after a print Useful when the wrong spool was assigned or the print was canceled.</p>
      <img alt="Mobile change spool" src="docs/img/mobile_change_spool.PNG" />
    </td>
  </tr>
</table>

</details>

---

## What you need

- Android Phone with Chrome web browser or iPhone (manual process much more complicated if using NFC Tags)
- Server to run OpenSpoolMan with https (optional when not using NFC Tags) that is reachable from your Phone and can reach both SpoolMan and Bambu Lab printer on the network
- Bambu Lab printer in **LAN-only / Developer Mode** — https://eu.store.bambulab.com/collections/3d-printer
- SpoolMan installed — https://github.com/Donkie/Spoolman
- NFC Tags (optional) — https://eu.store.bambulab.com/en-sk/collections/nfc/products/nfc-tag-with-adhesive

---

## How to setup

<details>
<summary>Python / venv deployment (see Environment configuration below)</summary>

1. Clone the repository:
   ```bash
   git clone https://github.com/diegoeloren/openspoolman.git
   cd openspoolman
   ```
2. Create and activate a virtual environment, then install the dependencies:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```
3. Configure the environment variables (see below).
4. Run the server with:
   ```bash
   python wsgi.py
   ```
   OpenSpoolMan listens on port `8001` by default so it does not clash with SpoolMan on the same host.

</details>

<details>
<summary>Docker deployment (see Environment configuration below)</summary>

1. Make sure `docker` and `docker compose` are installed.
2. Configure the environment variables (see below).
3. Copy `docker-compose.yaml` to your deployment directory and adjust any host volumes or ports as needed.
4. Build and start the containers:
   ```bash
   docker compose up -d
   ```

</details>

<details>
<summary>Kubernetes (Helm) deployment (see Environment configuration below)</summary>

1. Use the bundled Helm chart under `./helm/openspoolman`:
   ```bash
   helm dependency update helm/openspoolman
   ```
2. Create a `values.yaml` (or use `helm/openspoolman/values.yaml`) that overrides the same `config.env` entries and configures an ingress with TLS for your cluster.
3. Install or upgrade the release:
   ```bash
   helm upgrade --install openspoolman helm/openspoolman -f values.yaml --namespace openspoolman --create-namespace
   ```
4. Verify the pods and ingress:
   ```bash
   kubectl get pods -n openspoolman
   kubectl describe ingress -n openspoolman
   ```

</details>

### Environment configuration

Rename `config.env.template` to `config.env` or set environment properties:

- `OPENSPOOLMAN_BASE_URL` — the HTTPS URL where OpenSpoolMan will be available on your network (no trailing slash, required for NFC writes).
- `PRINTER_ID` — find it in the printer settings under Setting → Device → Printer SN.
- `PRINTER_ACCESS_CODE` — find it in Setting → LAN Only Mode → Access Code (the LAN Only Mode toggle may stay off).
- `PRINTER_IP` — found in Setting → LAN Only Mode → IP Address.
- `SPOOLMAN_BASE_URL` — the URL of your SpoolMan installation without trailing slash.
- `AUTO_SPEND` — set to `True` to enable filament tracking (required for any tracking to work).
- `TRACK_LAYER_USAGE` — set to `True` to enable per-layer tracking and consumption **while `AUTO_SPEND` is also `True`**. If `AUTO_SPEND` is `False`, all filament tracking remains disabled regardless of `TRACK_LAYER_USAGE`.
- `DISABLE_MISMATCH_WARNING` — set to `True` to hide mismatch warnings in the UI (mismatches are still detected and logged to `logs/filament_mismatch.json`).
- `CLEAR_ASSIGNMENT_WHEN_EMPTY` — set to `True` if you want OpenSpoolMan to clear any SpoolMan assignment and reset the AMS tray whenever the printer reports no spool in that slot.
- `COLOR_DISTANCE_TOLERANCE` — integer (default `40`), perceptual ΔE threshold for tray/spool color mismatch warnings.

By default, the app reads `data/3d_printer_logs.db` for print history; override it through `OPENSPOOLMAN_PRINT_HISTORY_DB`.

### SpoolMan setup

Run SpoolMan and add these extra fields:

- **Filaments**
  - `type` — Choice: `AERO,CF,GF,FR,Basic,HF,Translucent,Aero,Dynamic,Galaxy,Glow,Impact,Lite,Marble,Matte,Metal,Silk,Silk+,Sparkle,Tough,Tough+,Wood,Support for ABS,Support for PA PET,Support for PLA,Support for PLA-PETG,G,W,85A,90A,95A,95A HF,for AMS`
  - `nozzle_temperature` — Integer Range, °C, 190–230
  - `filament_id` — Text
- **Spools**
  - `tag` — Text
  - `active_tray` — Text

Add your Manufacturers, Filaments and Spools to SpoolMan (consider 'Import from External' for faster workflow).

The filament id lives in `C:\Users\USERNAME\AppData\Roaming\BambuStudio\user\USERID\filament\base` (same for each printer/nozzle).

### SpoolMan stickers

SpoolMan can print QR-code stickers for every spool; follow the [SpoolMan label guide](https://github.com/Donkie/Spoolman/wiki/Printing-Labels) to generate them. Before printing, update the base URL in SpoolMan's settings to point at OpenSpoolMan so every sticker redirects to OpenSpoolMan instead of SpoolMan.

---

## Filament matching rules

- The spool's `material` must match the AMS tray's `tray_type` (main type).
- For Bambu filaments, the AMS reports a sub-brand; this must match the spool's sub-brand. You can model this either as:
  - `material` = full Bambu material (e.g., `PLA Wood`) and leave `type` empty, **or**
  - `material` = base (e.g., `PLA`) and `type` = the add-on (e.g., `Wood`).
- You can wrap optional notes in parentheses inside `material` (e.g., `PLA CF (recycled)`); anything in parentheses is ignored during matching.
- If matching still fails, temporarily hide the UI warning via `DISABLE_MISMATCH_WARNING=true` (mismatches are still logged to `logs/filament_mismatch.json`).

---

## With NFC Tags

- For non-Bambu filament, select it in SpoolMan, click 'Write,' and tap an NFC tag near your phone (allow NFC).
- Attach the NFC tag to the filament.
- Load the filament into AMS, then bring the phone near the NFC tag so it opens OpenSpoolMan.
- Assign the AMS slot you used in the UI.

## Without NFC Tags

- Click 'Fill' on a tray and select the desired spool.
- Done.

---

## Accessing OpenSpoolMan

Once the server is running, open `https://<host>:8443` if you used the built-in adhoc SSL mode, or `http://<host>:8001` when the service listens on the default port 8001. For Docker deployments, you can also use `docker compose port openspoolman 8001` to see the mapped host port.

---

## Notes

- If you change the `OPENSPOOLMAN_BASE_URL`, you will need to reconfigure all NFC tags.
- Cloud prints are not tested or supported in this fork.

---

## TBD / Known gaps

- Filament remaining in AMS (AMS Lite only — no full AMS tested yet)
- Checkpoint resume: spool→tray bindings are re-learned live after restart; usage between crash and first SETTLED event is buffered and applied once the tray is confirmed
- Multi-AMS setups: architecture supports it (tray IDs 0–3 = AMS 1, 4–7 = AMS 2), but not yet tested
- LOCAL jobs from printer internal storage (`file:///userdata/` paths other than `project_file.gcode`) — MQTT recordings needed to extend detection
- Video showcase
