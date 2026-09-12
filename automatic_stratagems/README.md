# Automatic stratagems

Automatic stratagems scans the open Helldivers 2 selection screen or expanded
mission menu and maps recognized stratagems to Stream Deck buttons. The feature
is optional and off by default. Ordinary stratagem buttons continue to work
without its dependencies.

## Setup

The button capture choices are Automatic, Gamescope, and Screenshot. Earlier live checks
covered Steam F12 and native Gamescope on StreamController 1.5.0-beta.15 Flatpak,
Hyprland, GNOME 50, x86_64, CPython 3.13, and the English game UI at 5120×2160.
The configurable trigger workflow requires its own live desktop verification;
fixture tests do not establish support for every screenshot shortcut or script.

1. Install the plugin from the StreamController store.
2. Open **Settings → Plugins → HELLDIVERS 2** and switch on **Automatic
   stratagems**. Switching it on prepares the scanner runtime in the background
   and the **Scanner setup** row reports the result. Nothing is installed while
   the feature is off.

   **Flatpak:** downloads and verifies the locked OCR payload and validates the
   sandbox-provided NumPy 2.2.3, OpenCV 4.11.0, Pillow 11.1.0, and evdev 1.9.1.

   **Native:** builds `automatic_stratagems/.venv` with StreamController's own
   interpreter and installs the pinned `automatic_stratagems/requirements.txt`
   into it. `evdev` publishes no wheels, so a C compiler and Linux kernel
   headers must be present. This environment belongs to the feature and is
   rebuilt whenever it cannot be verified; the repository's root `.venv` is a
   separate developer environment for the asset updater and tests.

   StreamController runs the same preparation from `__install__.py` after every
   store install and update, before the plugin loads, whenever the feature is
   already switched on. An update replaces the plugin directory, so the runtime
   is rebuilt then. Preparation never runs at plugin startup or during a scan: a
   scan started before the runtime is ready prepares it instead of scanning and
   asks for another scan. **Run setup** in the settings row prepares or repairs
   it at any time, and records the outcome in ignored
   `automatic_stratagems/runtime/setup-status.json`.

3. Install the helpers required by the selected source:

   - native Gamescope: host `gamescopectl` and a uniquely associated Helldivers Gamescope session
   - Flatpak Gamescope: Steam’s matching `org.freedesktop.Platform.VulkanLayer.gamescope`
     extension and host `flatpak`; live compatibility is **unverified**
   - Screenshot hotkey: `evdev`, access to `/dev/uinput`, and a shortcut that saves an image
   - Screenshot script: an executable script readable from the sandbox and runnable on the host
   - native optional mission-name fallback: Tesseract with English data;
     Flatpak setup supplies and requires its locked OCR payload

   Flatpak host transport also requires sandbox `flatpak-spawn`, session-bus
   access to `org.freedesktop.Flatpak`, host `/usr/bin/grep`, `/usr/bin/mv`,
   `/usr/bin/rm`, `/usr/bin/sh`, `/usr/bin/sleep`, and `/usr/bin/sync`, plus
   readable host `/proc` uptime and process metadata.
   Gamescope additionally requires host `/usr/bin/python3` for bounded identity
   and file-mapping checks. For Flatpak Gamescope, those checks guard entry into
   the selected sandbox before invoking its capture CLI. Host Python never runs
   recognition or OCR. Native StreamController also requires `/usr/bin/setsid`
   to isolate and reap these capture commands.

   Flatpak setup validates sandbox-provided NumPy 2.2.3, OpenCV 4.11.0, Pillow
   11.1.0, and evdev 1.9.1 rather than copying a host Python environment. Its
   11.1 MB locked payload contains:

   | Component | Purpose | Payload | License |
   | --- | --- | ---: | --- |
   | Tesseract 5.5.0 and English `tessdata_fast` 4.1.0 | mission-name fallback | 4.17 MB | Apache-2.0 |
   | libtesseract 5.5.0 and Leptonica 1.86.0 | OCR engine and image library | 6.91 MB | Apache-2.0 and BSD-2-Clause |

   License notices are retained in each generated profile. The smaller English
   `tessdata_fast` model keeps the payload bounded; its OCR results can differ
   from native system models. Native installs retain their existing optional
   Tesseract and English data. Neither profile changes recognition thresholds.

### Manual and from-source installs

StreamController runs `__install__.py` only for plugins it installs itself,
including custom entries under **Settings → Store → Custom Plugins**, which
accept a repository URL and branch. A plugin directory copied or cloned by hand
never runs it. Run it once yourself, from the installed plugin directory, with
the interpreter that runs StreamController:

```sh
PLUGIN_ROOT="$HOME/.var/app/com.core447.StreamController/data/plugins/net_jslay_helldivers_2"
flatpak run --command=/usr/bin/python3 com.core447.StreamController \
  "$PLUGIN_ROOT/__install__.py"
```

```sh
# Native StreamController, using its own interpreter
/path/to/StreamController/venv/bin/python "$PLUGIN_ROOT/__install__.py"
```

The hook prepares nothing unless automatic stratagems are switched on in the
plugin settings, and it always exits successfully so a failed preparation cannot
fail a plugin installation. It reports what it did on standard output and
records the same outcome in `automatic_stratagems/runtime/setup-status.json`.

`automatic_stratagems/tools/setup-runtime` still drives the Flatpak profile
explicitly, including offline artifacts and rollback:

```sh
flatpak run --command=/usr/bin/python3 com.core447.StreamController \
  "$PLUGIN_ROOT/automatic_stratagems/tools/setup-runtime" \
  --root "$PLUGIN_ROOT" setup
flatpak run --command=/usr/bin/python3 com.core447.StreamController \
  "$PLUGIN_ROOT/automatic_stratagems/tools/setup-runtime" \
  --root "$PLUGIN_ROOT" check
```

`setup` downloads hash-pinned sources when absent, validates real imports and
OCR, then atomically activates the new profile. Add `--artifact-dir DIR
--offline` after `setup` to use sandbox-accessible downloaded sources. Replace
`check` with `rollback` to verify and activate the previous profile. Generated
profiles and downloads live in ignored `automatic_stratagems/runtime/`.

### Gamescope inside Flatpak Steam

Flatpak Gamescope support is **implemented but not live-verified**. It uses the
same Gamescope choice; Automatic can fall back to Screenshot when capture fails.

Install the Gamescope extension matching Steam’s Freedesktop runtime branch and
configure the game to launch through it, typically `gamescope -- %command%`.
The extension supplies `gamescopectl` inside Steam’s sandbox. See the
[extension documentation](https://github.com/flathub/org.freedesktop.Platform.VulkanLayer.gamescope)
for setup and compatibility-tool limitations.

The scanner identifies the game’s Gamescope sandbox and invokes its capture CLI
through `flatpak enter`. This requires unprivileged namespace entry and a writable
Steam cache with a verified mapping inside its sandbox. Missing access or
ambiguous process/socket identity causes capture to fail;
the plugin does not change Flatpak permissions or request root access.

Flatpak normally hides another application’s private cache, including Steam’s,
even when home-directory access is enabled. A bounded host file operation waits
for the new capture and copies its encoded bytes into StreamController’s shared
job directory. It does not decode or process the image. Temporary-file cleanup
uses the recorded file/directory identities. If cleanup cannot be confirmed, the
scan stops without Screenshot fallback and retains its job directory and
`steam-capture.json` recovery record. A forced application exit can also leave
temporary files; there is no automatic recovery sweep.

Live capture, HDR output, Steam/Proton compatibility and cleanup in that setup
remain unverified. Fixture and dummy-command tests do not establish those gates.

### Flatpak process split

Recognition, worker pools, Tesseract, hotkey input, accessible screenshot polling,
caching, and diagnostics stay inside StreamController’s Flatpak. The bounded host
runner invokes native Gamescope capture, minimal process/socket/file operations,
and user-selected screenshot scripts. For Flatpak Gamescope it invokes the
capture CLI inside the identified Steam sandbox through `flatpak enter`. Host
scripts are ordinary executable programs: choose scripts you trust. No host Python
recognizer is required.

Each host command, its descendants, watchdog, and guard share one owned process
group. The guard writes durable `ready` and `result` markers, then kills that
group. A separate bounded host query must prove it has no non-zombie members
before the parent records `complete`. A result alone is not cleanup proof.
Queries retry on unknown state, and input ownership remains held while cleanup is
unconfirmed. Gamescope host metadata verifies process/socket association and shared paths.

If a launcher dies after submission but before `ready`, the parent waits for the
absolute shared-kernel deadline, then requires an exact host nonce-absence
query. A request delivered after that query refuses to launch because it is
expired. This rare path can hold input for the remaining 120-second scan limit.
Clock, query, or ownership-state errors keep cleanup unknown beyond that limit.

### Configure screenshots

Configure screenshots once in **Settings → Plugins → HELLDIVERS 2**.
**Scan workers** controls recognition parallelism (1–32, default 2). Lower it on
memory-constrained systems; capture and key input remain serialized.

Expand **Screenshot capture** to configure:

- **Trigger:** Key by default, or Script
- **Keycode:** `KEY_F12` by default; enter evdev names joined with `+` for a chord,
  such as `KEY_LEFTCTRL+KEY_F12`. This is a keycode text field
- **Script:** one absolute executable path; it must save the screenshot and exit.
  Arguments and shell commands are not accepted in this field
- **Screenshot folder:** leave empty to use the Helldivers Steam screenshot folder,
  or browse for a folder. Choose explicitly if several Steam accounts have captures
- **Delete after successful scan:** on by default; removes the newly captured image
  and, for Steam screenshots, its exact matching thumbnail

The folder and script must be readable from StreamController's sandbox. Typing
a path grants no access. Configure a hotkey or script that saves an image containing
only the complete game window. The plugin does not check which application is
focused. For hotkeys, focus the game before scanning. If using HDR, Gamescope or
Steam in-game screenshots are preferred. Steam in-game screenshots require the
Steam overlay to be enabled.

The scanner accepts one stable new or changed PNG/JPEG directly inside the folder.
Ambiguous captures, symlinks, and incomplete images are rejected. Keep folders
within 4,096 entries. Scripts should write under a temporary name and rename the
completed image into place.

Deletion applies only after successful recognition to newly created files that
still match the captured identities. Existing, overwritten, failed, or cancelled
captures are retained. Steam cleanup pairs the main image with its same-name file
inside `thumbnails/`; uncertain or incomplete pairs are retained. Steam's screenshot
index is not edited. Turn deletion off to keep screenshots.

### Choose a capture source

Each scan button has one capture selector:

- **Automatic** (default): try Gamescope first, then Screenshot if Gamescope fails
  or recognition is incomplete. Keep the Gamescope result if the fallback recognizes
  no more stratagems
- **Gamescope:** capture only the uniquely associated Helldivers Gamescope session
- **Screenshot:** use the shared screenshot settings directly

All buttons, including generated pages, use the current plugin screenshot settings.
Per-button screenshot paths and triggers are no longer used. Changing shared settings
cancels active scans and invalidates stale capture bindings. Group and page behavior
remain unchanged; failed replacement scans preserve saved screenshot-backed pages.

## Actions

For a generated layout, drag **Automatic Stratagem Page** onto the deck. To fill
an existing page, add **Automatic Stratagem Scanner** and **Automatic Stratagem**
buttons with the same group; leave slots at `-1` for automatic numbering. Open
the supported game menu, then tap the page or scanner button.

| Action | Tap | Hold |
| --- | --- | --- |
| **Automatic Stratagem Page** | Scan, create, and open a page when no cache exists; otherwise reopen it without scanning | Scan and open a replacement cache |
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
a generated page seeds that page from the scan without changing the source page.
Later scans and clears on either page do not change the other page's assignments.
Each hotkey or script capture proves freshness from its own before-and-after folder
snapshot; GUI actions do not share last-image history.

Set each Automatic Stratagem button's color filter to Any, Red, Blue, Green, or
Yellow. The scanner fills that slot only with an allowed icon color; it leaves
uncertain matches empty.

Each Automatic Stratagem Page button owns a separate cache, even when several
buttons use the same group. Holding one page button leaves other page caches and
source assignments unchanged. Regeneration always preserves the old page on
setup failure. **Gamescope** deletes its cache after successful preflight and
before capture; a later capture or recognition failure leaves no replacement.
**Automatic** and **Screenshot** keep the old page until a successful replacement
is ready. Correct the error and tap or hold again to retry.

Existing **Automatic Stratagem Scanner** buttons configured for the former
new-page mode keep compatible page-opening behavior.

## Troubleshooting

- **Automatic actions are absent:** enable the feature, then reopen the action
  chooser. If setup is incompatible, ordinary actions remain available.
- **Scan fails immediately:** open the initiating button's settings and read
  **Last scan**. A first failure caused by a missing runtime prepares it and
  asks for another scan. Otherwise read **Scanner setup** in the plugin settings
  and press **Run setup**; check the selected backend's helpers as well. Flatpak
  users can run the setup tool's `check` command, then `setup` or `rollback`
  when the active profile is invalid. A page button keeps its latest attempt separately from source-page
  assignments; the attempt is not retained across app restarts.
- **Preparation fails while downloading:** the locked Flatpak sources come from
  snapshot.debian.org, which throttles repeated requests, so one attempt can
  report a download failure. Verified downloads are kept in
  `automatic_stratagems/runtime/downloads/`, so pressing **Run setup** again
  resumes from them rather than starting over.
- **Automatic position unavailable:** the host page structure could not resolve
  this button's position. Automatic slots stop rather than sharing slot 1;
  explicit positive slots remain usable.
- **Partial result:** one or more rows were unknown, unconfirmed, or reported as
  partial. Check uncertain assignments and scan the intended game screen again.
  Capacity overflow alone does not make a scan partial.
- **Steam capture fails:** verify `/dev/uinput` access, the configured keycode,
  and Steam overlay. Steam creates the screenshot in its folder; successful safe
  cleanup removes it and its thumbnail when deletion is enabled.
- **Flatpak transport fails:** the current scan retries cleanup checks and blocks
  new input until cleanup is confirmed. Restore the named helper, `/proc`, or
  session-bus access while the app remains running. A submitted command without
  `ready` can hold input for up to 120 seconds; unavailable metadata or malformed
  state can keep it blocked longer. A result, wrapper exit, or restart does not
  reconcile an old host-job registry.
- **Back cannot find its source:** the generated page remains recoverable rather
  than switching to an unrelated page.
- **Disabling the feature:** cancels scans and blocks automatic actions while
  preserving configured buttons, caches, and assignments.

The calibrated recognition profile is the English UI at 5120×2160. Included
fixtures cover known screens and bounded geometry changes; they do not certify
other resolutions, aspect ratios, HUD settings, HDR pipelines, unseen multiplayer
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
metadata, limits, and the distinction between calibration and held-out captures.

Run the feature and ordinary key-mapping regressions with:

```sh
.venv/bin/python automatic_stratagems/tools/check
```

Check that the install hook, runtime provisioning, and host scripts import only
the standard library:

```sh
python3 automatic_stratagems/tools/check-imports
```

Validate the Flatpak boundary with isolated data and saved fixtures:

```sh
python3 automatic_stratagems/tools/check-sandbox \
  --artifact-dir /path/to/runtime/downloads \
  --fixture-manifest /private/calibration/manifest.json \
  --fixture-case mission.png \
  --full-suite --translations --calibration --benchmark
```

Use downloaded artifacts from `setup-runtime` and a curated 5120×2160 English
mission fixture with a known OCR fallback. The tool stages a separate plugin
copy in the app cache, uses offline setup, and substitutes fixture bytes for
capture. It checks generated-page completion, cancellation, host descendants,
input-lock release, and actual sandbox OCR execution. Results include source
hashes, process locations, replay outputs, and cache-cold/warm timing and memory.
It does not change the installed plugin or send game input. These checks do not
establish live desktop or hardware support.

Run the separate **required transport gate** on the host when changing Gamescope
namespace entry or host-command ownership:

```sh
HD2_RUN_FLATPAK_ENTER_TRANSPORT=1 .venv/bin/python -m unittest -v \
  automatic_stratagems.tests.test_flatpak_enter_transport
```

This starts disposable command-only StreamController Flatpaks and harmless dummy
commands. It verifies success, timeout, cancellation, output overflow, wrapper
death, descendant reaping, and the target sandbox's survival. It needs the
installed StreamController Flatpak and session-bus access. Neither `check` nor
`check-sandbox` runs this gate automatically; unavailable prerequisites are
**BLOCKED**, and a skipped test is not transport evidence. It does not capture the
screen, inject input, restart the app, or verify real Flatpak Steam/Gamescope.

See [Architecture](docs/architecture.md) for component boundaries, persistence,
process ownership, and extension points.
