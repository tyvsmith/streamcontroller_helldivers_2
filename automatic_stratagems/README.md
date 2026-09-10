# Automatic stratagems

Automatic stratagems scans the open Helldivers 2 selection screen or expanded
mission menu and maps recognized stratagems to Stream Deck buttons. The feature
is optional and off by default. Ordinary stratagem buttons continue to work
without its dependencies.

## Setup

**Platform limit:** saved-report page and action flows have been exercised with
native StreamController 1.5.0-beta.15; other releases are unverified. Capture
routes cover Hyprland, window-sharing portals on Wayland, and X11. New desktop
routes require live validation on your setup. Flatpak scans remain blocked
because forced cleanup cannot guarantee termination of host descendants.

1. Open **Settings → Plugins → HELLDIVERS 2** and enable **Automatic
   stratagems**.
2. From the installed plugin directory, reuse its single root `.venv`. Create it
   only when absent, using Python 3.12 or newer, then install the pinned scanner
   dependencies:

   ```sh
   python -m venv .venv  # only when absent
   .venv/bin/python -m pip install -r automatic_stratagems/requirements.txt
   .venv/bin/python -m pip check
   ```

   The same environment can serve the asset updater. If it has no `pip`, run
   `.venv/bin/python -m ensurepip --upgrade`; install the distribution's Python
   `venv` package first if `ensurepip` is unavailable. Dependencies are never
   installed during plugin startup. Store updates may replace the plugin
   directory, so repeat this setup after an update when `.venv` is absent.
3. Install the helpers required by the capture path:

   - Hyprland: `hyprctl`; desktop capture also needs `grim`
   - Wayland portal: a desktop portal offering window sharing, `gst-launch-1.0`,
     and GStreamer's PipeWire, video conversion, and PNG plugins
   - X11: `xprop` and ImageMagick's `import`
   - Gamescope: `gamescopectl`
   - Steam: `evdev`, access to `/dev/uinput`, and the Steam F12 screenshot binding
   - optional mission-name fallback: Tesseract with English data

### Choose a capture route

**Automatic** uses the existing Gamescope/Steam/desktop sequence on Hyprland,
window sharing on other Wayland desktops, and Gamescope/Steam/X11 on X11.
Button settings default to **Use plugin default**, which follows later changes
to the plugin setting. Choose **Automatic** or a specific backend to pin that
button. Generated pages retain the effective capture setting from creation.

For **Portal**, choose the Helldivers 2 window in the desktop's sharing dialog.
The portal may remember your choice; revoked permissions or an unavailable window
can require selection again. This route reads one frame from the selected window
without pressing F12. It requires window sharing and does not fall back to
capturing your whole monitor.

## Actions

For a generated layout, drag **Automatic Stratagem Page** onto the deck. To fill
an existing page, add **Automatic Stratagem Scanner** and **Automatic Stratagem**
buttons with the same group; leave slots at `-1` for automatic numbering. Open
the supported game menu, then tap the page or scanner button.

| Action | Tap | Hold |
| --- | --- | --- |
| **Automatic Stratagem Page** | Scan, create, and open a page when no cache exists; otherwise reopen it without scanning | Delete its cache, scan, and open a replacement |
| **Automatic Stratagem Scanner** | Replace assignments for the current page and group | Clear assignments for the current page and group |
| **Automatic Stratagem** | Execute an assignment, or scan its group when empty | Scan its group, assigned or empty |
| **Back** | Return to the source and retain the generated page | — |

A **+** on Automatic Stratagem Page marks a missing cache; the plain icon marks
an existing cache. Back retains the cache across app restarts and remains
available while automatic scanning is disabled. Back is generated with the page
and is hidden from the action chooser.

Open the desired game screen before scanning; the plugin does not open it. Only
the button that starts a scan animates or shows a failure triangle. Page and
scanner buttons keep the center label blank. A question-mark badge on an empty
Automatic Stratagem slot means the latest completed scan was partial.
Unconfirmed badges mean a previous assignment was not confirmed by the latest
scan; tapping still executes that previous assignment. Badges do not represent
cooldown state. Check uncertain assignments before use and rescan the intended
menu when needed. Button settings explain the
current assignment and uncertainty in text. A tap rejected while input is busy
gives brief feedback on that button only.

### Slots and groups

Leave **Slot** at `-1 (automatic)` to allocate unique positive numbers in stable
page order. Explicit positive slots are reserved first. Moving a button may
change its automatic slot and gives an Automatic Stratagem Page button a new
cache.

Groups match exactly within the same deck and page. Finish a group edit with
Apply, Enter, or leaving the field; typing alone does not change assignments or
cancel a scan. Assignments, scans, and clears apply only to that context. Creating
a generated page seeds that page from the scan without changing the source page. Later scans and clears on either page do not
change the other.

Set each Automatic Stratagem button's color filter to Any, Red, Blue, Green, or
Yellow. The scanner fills that slot only with an allowed icon color; it leaves
uncertain matches empty.

Each Automatic Stratagem Page button owns a separate cache, even when several
buttons use the same group. Holding one page button leaves other page caches and
source assignments unchanged. A failed regeneration leaves no replacement, so
tap or hold again after correcting the error.

Existing **Automatic Stratagem Scanner** buttons configured for the former
new-page mode keep compatible page-opening behavior.

## Troubleshooting

- **Automatic actions are absent:** enable the feature, then reopen the action
  chooser. If setup is incompatible, ordinary actions remain available.
- **Scan fails immediately:** open the initiating button's settings and read
  **Last scan**. Check `.venv`, pinned dependencies, and the selected backend's
  helpers. A page button keeps its latest attempt separately from source-page
  assignments; the attempt is not retained across app restarts.
- **Automatic position unavailable:** the host page structure could not resolve
  this button's position. Automatic slots stop rather than sharing slot 1;
  explicit positive slots remain usable.
- **Partial result:** one or more rows were unknown, unconfirmed, or reported as
  partial. Check uncertain assignments and scan the intended game screen again.
  Capacity overflow alone does not make a scan partial.
- **Steam capture fails:** verify `/dev/uinput` access and the F12 binding. Steam
  capture presses F12 and leaves the screenshot in Steam storage.
- **Back cannot find its source:** the generated page remains recoverable rather
  than switching to an unrelated page.
- **Disabling the feature:** cancels scans and blocks automatic actions while
  preserving configured buttons, caches, and assignments.

The calibrated recognition profile is the English UI at 5120×2160. Included
fixtures cover known screens and bounded geometry changes; they do not certify
other resolutions, aspect ratios, HUD settings, HDR pipelines, multiplayer
layouts, or gameplay conditions. Ambiguous observations remain unknown.
Cold recognition can use several GiB of memory; low-memory hosts are not
validated.

## Replay, diagnostics, and tests

Replay a saved image through the same recognizer used by the plugin:

```sh
.venv/bin/python -m automatic_stratagems.scanner \
  --image /path/to/capture.png --json
```

Routine scans retain no diagnostic screenshots. Add
`--debug-dir /private/path` for explicit troubleshooting artifacts; review raw
captures before sharing and keep them out of Git.

### Evaluate a screenshot collection

Keep full-scene captures in a private directory. Prepare a manifest beside that
capture directory, then fill in each case's provenance, split, detected mode,
and ordered expected catalog IDs (`null` for an unknown row):

```sh
.venv/bin/python automatic_stratagems/tools/fixture-manifest prepare \
  /private/evaluation/captures --output /private/evaluation/manifest.json
# Edit the incomplete manifest before validation.
.venv/bin/python automatic_stratagems/tools/fixture-manifest validate \
  /private/evaluation/manifest.json
.venv/bin/python automatic_stratagems/tools/evaluate-fixtures \
  /private/evaluation/manifest.json --split heldout \
  --output /private/evaluation/results.json
```

Use `calibration` for screenshots used to tune recognition. Reserve `heldout`
for independently labeled captures never used for tuning. Evaluation checks
hashes, dimensions, labels, provenance, and known overlap before running each
selected image through the scanner without caching. Results compare detected
mode and ordered IDs, including unknown rows; mismatches return a failing status.
Hash checks cannot establish independence on their own.

Tools leave source images untouched and refuse to overwrite output files. They
neither capture the desktop nor add files to Git. Full scenes may contain private
information; review and curate them separately before sharing. See the
[manifest contract](docs/architecture.md#offline-fixture-evaluation) for required
metadata and limits.

Run the feature and ordinary key-mapping regressions with:

```sh
.venv/bin/python automatic_stratagems/tools/check
```

See [Architecture](docs/architecture.md) for component boundaries, persistence,
process ownership, and extension points.
