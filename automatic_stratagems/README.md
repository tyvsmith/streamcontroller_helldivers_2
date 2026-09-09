# Automatic stratagems

Scan the open Helldivers 2 selection screen or expanded mission menu into numbered
Stream Deck slots. Enable **Settings → Plugins → HELLDIVERS 2 → Enable automatic
stratagems** to use the feature. It defaults to off; ordinary buttons do not
require scanner setup.

**Flatpak scanning is currently unavailable:** forced cancellation could leave host
children running. The feature rejects Flatpak scans before launch. Native process
cleanup is tested; fresh installed-app and live-game validation remain blocked.

## Host setup

From the installed plugin directory, create or reuse its single root environment:

```sh
python -m venv .venv  # only when absent
.venv/bin/python -m pip install -r automatic_stratagems/requirements.txt
.venv/bin/python -m pip check
```

Use host Python 3.12 or newer. The same environment may serve the asset updater;
install scanner requirements explicitly and resolve any dependency conflict before
scanning. Dependencies are never installed at plugin startup. Store installs and
updates replace the whole plugin directory, including `.venv`; repeat this setup
afterward. Recreate an incompatible environment after a host
Python update. Ordinary buttons remain usable without scanner setup.

The runner launches `.venv/bin/python -m automatic_stratagems.scanner` from the
installed plugin root. No root shell launcher, separate scanner environment or
development-checkout setting is required.

Live capture currently requires Linux/Hyprland and `hyprctl` for game identity.
Gamescope capture uses `gamescopectl`; desktop capture uses `grim`. Steam capture
requires `evdev`, `/dev/uinput` access and the Steam F12 screenshot binding; it
presses F12 and leaves Steam's screenshot in Steam storage. Optional name fallback
uses Tesseract with English data. Preflight checks Python dependencies, `hyprctl`
and helpers for an explicitly selected backend. Automatic capture reports
per-backend failures and may try the next backend.

## Buttons and recovery

- tap **Auto Stratagems** to create, scan and open a generated page; subsequent taps
  reopen that same cached page without scanning
- **+** on Auto Stratagems means no cached page; the plain page icon means one exists
- **Back** returns to the source and retains the page, including across app restarts
- hold **Auto Stratagems** to delete its generated page and saved page assignments;
  source-page assignments remain
- tap **Scan Stratagems** to replace the current group from a fresh scan; hold to clear
- tap **Auto Stratagem** to execute its assignment; hold to scan its group
- leave slot at **-1 (automatic)** to assign distinct numbers in page order, skipping
  explicit slot numbers; choose a positive number to pin a slot
- configure Any/Red/Blue/Green/Yellow filters; groups isolate assignments by deck
  and page, with linked source/temporary pages sharing observations
- existing scanner buttons configured for new-page mode behave as Auto Stratagems;
  add the separate actions from the chooser for new buttons
- open the desired game menu yourself; scanning does not open it for you
- keep unknown slots disabled; recognized IDs use existing catalog sequences
- inspect **Last scan** for failures; Partial means unknown or unconfirmed results,
  not extra recognized stratagems beyond page capacity
- restored/unconfirmed badges are not cooldown indicators
- disable scanning to preserve configured buttons and assignments while blocking scan actions

All key actions are excluded while a scan owns the input lock. Cancellation must
stop host work before ordinary key actions resume. Back remains available with
scanning disabled; a missing source page must leave the generated page recoverable.
See [process ownership](docs/contracts.md) and [saved state](docs/state.md).

## Support and evidence

The recognition regression profile is English UI at 5120×2160. Tests include
calibration captures and synthetic geometry changes; they do not establish
independent accuracy. The [fixture manifest](tests/fixtures/manifest.json) records
known provenance, expectations and missing evidence. Other layouts, real
multiplayer selection, aspect ratios, HUD settings and HDR pipelines remain
unsupported until independently verified.
Ambiguous observations must remain unknown. Cold recognition can use several GiB
of RAM; low-memory hosts have not been validated.

Fresh installed-app behavior, visible beta.15 chooser/page lifecycle, real-button
input and live capture remain separate verification gates. Offline tests do not
certify those combinations. Do not treat a successful helper launch as UI or
hardware evidence. This feature remains experimental until those gates pass.

## Validation and explicit diagnostics

```sh
.venv/bin/python automatic_stratagems/tools/check
.venv/bin/python -m automatic_stratagems.scanner --image /path/to/capture.png --json
```

The normal check runs feature and ordinary key-mapping regressions without game
input. Replay uses the same recognizer as StreamController. Routine scans retain
no diagnostic screenshots. Explicit `--debug-dir /private/path` enables capture
artifacts for troubleshooting; keep raw captures out of Git and review them before
sharing. Steam screenshots are a separate capture side effect.

`scan_runner.py` owns the process boundary; actions coordinate sessions and pages;
`scanner/` owns capture/recognition; `tests/` owns regression fixtures. Source,
requirements, tools, artwork and documentation for scanning stay in this folder.
