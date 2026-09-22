# Automatic stratagems architecture

Automatic stratagems maps an observed Helldivers 2 loadout to Stream Deck buttons.
It is optional and off by default. The scanner recognizes the open selection
screen or expanded mission menu; it does not track live cooldowns or open the
menu. Setup and usage are in the [feature README](../README.md).

## Ownership and integration

The feature implementation, tools, tests, and recognition references live under
`automatic_stratagems/`. Root `main.py` imports the guarded integration facade.
Ordinary, custom, and automatic assignments execute through root
`stratagem_execution.py`, which enforces their shared input lock. Ordinary action
behavior and settings remain intact; optional feature failures must not prevent
them from loading. Catalog keys and action IDs remain stable, and catalog
generation stays in `update/`.

| Component | Responsibility |
| --- | --- |
| `integration.py`, `visibility.py` | action registration, chooser visibility, runtime preparation threading, lifecycle hooks |
| `settings_rows.py` | Adwaita rows for the feature switch, scanner setup, workers, and screenshot capture, and the settings they save |
| `streamcontroller_adapter.py` | validated StreamController page action records, registration, and UI compatibility seams |
| `scan_actions.py` | action behavior and the coordinator facade it calls |
| `runtime_preparation.py` | runtime preparation for the settings button and failed scans, and the failed scan's message |
| `page_attempts.py` | each action's generated-page attempt record: start, session binding, finish, cancel |
| `scan_lifecycle.py` | scan start and preparation, active scans, worker execution, GTK completion, cancellation, disconnect, removal, shutdown |
| `scan_operation.py` | immutable scan plan, cancellation, worker ownership, constructor-bound finalization |
| `session_registry.py` | session lookup by action context, context identity, restore and persistence |
| `slot_reconciliation.py` | explicit slot reservation, automatic allocation from page topology, color filter reconciliation, redraw |
| `scan_session.py`, `scan_state.py` | assignment and filter state machine, validated atomic persistence |
| `generated_page_flow.py` | generated-page creation, cache reuse checks, Back, and deletion over `temporary_scan_page` |
| `temporary_scan_page.py`, `scan_artwork.py` | owned generated pages and action presentation |
| `capture_source.py` | capture defaults, normalized settings, binding identity, CLI arguments |
| `provision/` | runtime provisioning: `runtime_install` (single preparation path), `verify` (check and child preflight), `build` (install-time download, unpack, stage, activate, rollback), `runtime_profile`, `scanner_runtime` |
| `scan_runner.py`, `host_commands.py` | bounded scanner execution, host command ownership, cleanup, report validation |
| `scan_diagnostics.py` | runner-managed diagnostic runs: ownership markers, process identity, retention limits, pruning |
| `hostexec/` | scripts the host `/usr/bin/python3` runs outside the sandbox: Gamescope target lookup, Flatpak Steam capture and cleanup |
| `shared/` | standard-library helpers every context imports: atomic writes, bounded reads, stability polling, host-job identity, Gamescope target schema |
| `scanner/` | capture (`scanner/capture/`: bounded commands, hostexec launchers, screenshot trigger, detection, and cleanup), image decoding (`scanner/image_decode.py`), layout detection, recognition, OCR, caches, JSON reports |
| `scanner/capture_backends.py` | Automatic's Gamescope-then-Screenshot fallback order, keeping the attempt with more matched IDs, capture-quality rejection, and the Gamescope frame read |
| `scanner/recognize/` | recognition vocabulary: calibrated geometry and rim constants, the `Tile` crop, declared ranking keys and cached-ranking restore |
| `scanner/layout/` | pure layout transforms: the calibrated Ready-bar geometry that maps source pixels to matcher pixels |

## Execution contexts

The feature runs in five contexts with different dependency budgets.
`tools/check-imports` enforces standard-library-only imports, plus a short
list of permitted internal cross-imports, in five scopes:
`automatic_stratagems/__init__.py`, `shared/`, `hostexec/`, `provision/`, and
root `__install__.py`.

| Context | Interpreter | May import | Files |
| --- | --- | --- | --- |
| Plugin | StreamController's Python, with GTK | plugin modules | root `main.py`, feature modules |
| Scanner child | owned venv or Flatpak profile | OpenCV, NumPy, Pillow, evdev | `scanner/` |
| Host scripts | host `/usr/bin/python3`, outside the sandbox | stdlib, `shared/`, `hostexec/` | `hostexec/` |
| Install hook | StreamController's Python, before the plugin loads | stdlib, `shared/`, `provision/` | `__install__.py`, `provision/` |
| Tooling | developer interpreter | anything | `tools/`, `tests/` |

Some files cross those boundaries. `host_commands.py` is a plugin module that
also runs in the scanner child: `scan_game.py` and `scanner/capture/command.py`
both import it directly. The plugin also reaches into `scanner/`:
`scan_runner.py` imports `scanner/screenshot_capture.py`, which imports
`scanner/errors.py`, `scanner/limits.py`, and `scanner/capture/screenshot_*.py`.
The identity test's `PLUGIN_REACHABLE_SCANNER_FILES` names that exact list, so
none of those files may import GTK, cv2, or NumPy at module level. The scanner
child also imports `shared/` and `hostexec/__init__.py`.

`shared/` and the package `__init__.py` import only the standard library. The
install hook swallows every exception, so a stray import there would install
cleanly and never scan; the import check is what catches it. Each host script
inserts the plugin root on `sys.path` once before its first repository import,
and a test runs every script with `-I -S` to prove it loads.

StreamController imports the plugin as `plugins.<folder>.main` without the
repository root on `sys.path`, so plugin-side modules use relative imports.
Absolute `automatic_stratagems.` imports resolve only in the scanner child, host
scripts, the install hook, and tooling. `tests/test_plugin_package_identity.py`
imports plugin-side modules as `plugins.<id>.<module>` with the repository root
kept off `sys.path`, fails if `automatic_stratagems` is importable there, and
rejects absolute `automatic_stratagems.` imports in plugin-side files by AST. It
exists because `tools/check` and the other test runners put the root on
`sys.path`, which would otherwise hide those failures.

## Scan flow

1. Acquire the plugin's shared input lock.
2. Resolve the initiating action's deck, page, group, and current capture
   settings, and create a session token.
3. Run a non-destructive setup preflight, then launch the scanner child with the
   same cancellation event and an isolated runtime environment.
4. Capture and decode one image, locate supported menu rows, recognize them, and
   return a bounded JSON report.
5. Confirm scanner and host-process cleanup before the scanner runner returns;
   unconfirmed cleanup retains input ownership.
6. Queue completion on GTK's main loop. The callback may run before or after
   worker finalization; only worker finalization releases the input lock.
7. On GTK, recheck action presence, context, token, revision, feature state,
   and shutdown state before changing assignments.
8. Persist the affected session and redraw its actions.

Only the initiating button animates or displays scan failure. A busy rejection
changes that button's feedback without changing other assignments. Disabling the
feature cancels active scans and blocks automatic execution while retaining saved
assignments and generated pages; their Back action remains usable.

## Actions, contexts, and pages

| Action ID suffix | Tap | Hold |
| --- | --- | --- |
| `ScanStratagems` | scan current page/group | clear current page/group |
| `AutoStratagems` | reopen its cached page, or scan and create one | regenerate its page |
| `AutomaticStratagem` | execute its assignment, or scan if empty | scan its group |
| `TemporaryScanBack` | return to the recorded source page | no alternate action |

The Python classes are `AutomaticStratagemScanner`,
`AutomaticStratagemPage`, `AutomaticStratagem`, and `TemporaryScanBack`.
Registration retains the stable suffixes in the table for saved pages.

Older scanner buttons with `new_page` settings retain page-opening behavior.
Assigned execution uses the current catalog sequence and the root plugin's
modifier, direction, and timing settings.

A session belongs to one deck controller, action page path, and exact group.
Persistence uses deck serial, absolute page path, and group. Group edits apply
only when committed. Explicit positive slots are reserved first; `-1` slots are
allocated from authoritative page topology in row, column, state, and action-index
order. `streamcontroller_adapter.py` validates and sorts raw beta.15 action
records; `slot_reconciliation.py` holds group and slot policy. An unresolved automatic
position cannot scan or execute an assignment.
Release-time binding and revision checks prevent a press from executing a changed
assignment.

Generated pages write metadata version 4: owner, deck, source page, group, source
button address (input, state, action index), and capture-configuration identity.
Each page button owns its own cache; moving it changes that identity. Readers
validate versions 1–4. Versions 2–4 can reopen a cache by exact button ownership;
version 1 lacks that address and remains navigable without being rebound. Stored
capture settings are checked against their versioned identity before reuse.

Page creation uses a source-session token, preserves the source assignments, and
seeds a separate session on the generated page. Later scans and clears stay local
to their page. Regeneration always preserves the old page on setup failure.
Gamescope-only regeneration removes it after preflight and before capture;
later failure leaves no replacement. Automatic and Screenshot keep the old page
until a successful replacement is ready.

Back retains the page. Shutdown unregisters generated pages but retains their
files. Explicit deletion returns an active page to its recorded source before
removing owned files and state. Missing sources or uncertain ownership preserve
the page for recovery; uninstall retains cleanup failures.

## Capture and process locations

Buttons select Automatic (default), Gamescope, or Screenshot. Automatic tries Gamescope,
then Screenshot on capture failure or incomplete recognition. A usable Gamescope
result is replaced only if the fallback recognizes more IDs. Both attempts share
one deadline and input lock. Cancellation, an exhausted deadline, or unconfirmed
cleanup stops the operation. Screenshot preflight is deferred until it is needed.

In Flatpak, scanner orchestration, image decoding, recognition, worker pools,
Tesseract, UInput, accessible file polling, caches, and diagnostics remain inside
StreamController's sandbox. Host work is limited to capture commands, bounded
process/socket/file operations, and a configured screenshot script.

### Gamescope

`hostexec/host_metadata.py` runs on the host, launched by
`scanner/capture/gamescope.py`, and associates the Helldivers process with a
unique Gamescope socket using process ancestry and socket ownership. Ambiguity rejects capture;
there is no foreground-window check or arbitrary socket fallback.

- Native Gamescope: invoke its capture CLI through the bounded host runner
- Gamescope inside Flatpak Steam: verify the selected sandbox and path mapping,
  then invoke its capture CLI through `flatpak enter`

For Flatpak Steam, its private cache may be inaccessible to StreamController.
`hostexec/gamescope_flatpak.py` uses a bounded host file operation to wait for the new
capture and copy encoded bytes into the shared job directory. Host Python reads
metadata and bytes; it never decodes images or performs recognition. Cleanup
revalidates recorded file/directory identities. Unconfirmed cleanup stops fallback
and retains a recovery record. There is no automatic sweep of abandoned jobs.
The plugin does not change permissions or request elevated privileges.

### Screenshot

Plugin-wide settings choose a hotkey chord or one executable script, a screenshot
folder, and deletion after recognition. Defaults are `KEY_F12`, automatic Steam
folder discovery, and deletion enabled. Chords use evdev key names joined with
`+`. Scripts are invoked directly without shell parsing or configured arguments.
All scan buttons use current shared settings; changes invalidate active bindings.
GUI actions do not persist last-image fingerprints. Each triggered scan proves
freshness from its own before-and-after snapshot. Offline CLI capture retains its
previous-fingerprint and explicit rescan controls.

`scanner/screenshot_capture.py` orchestrates one capture: it snapshots the
directory, triggers the capture through `scanner/capture/screenshot_trigger.py`
(script or hotkey), and accepts one stable new or changed PNG/JPEG through
`scanner/capture/screenshot_detect.py`. `scanner/capture/screenshot_source.py`
resolves and validates the configured folder, and `scanner/capture/screenshot_files.py`
holds the bounded directory and fingerprint reads that detection and cleanup share.
With no configured trigger, it reads a stable file or folder through
`scanner/image_source.py` instead, honoring previous-fingerprint and rescan checks.
Blank paths resolve the Helldivers Steam screenshot directory for native or
Flatpak Steam; multiple candidates
require explicit selection. Symlinks, ambiguous captures, and incomplete reads
are rejected. Typing a path does not grant sandbox access. The user must configure
a trigger that saves the complete game window; the plugin does not check focus.

Key releases run on failure and cancellation. `scanner/capture/screenshot_cleanup.py`
owns deletion. It applies only when the
screenshot is the returned capture, at least one ID matched, and the newly
created file still has its captured identity. Automatic retains a fallback
screenshot when it keeps the Gamescope result. Existing or overwritten images
are retained. Steam cleanup claims the new image and its
new same-name thumbnail together; uncertain pairs are restored or retained.
Steam's screenshot index is not modified.

### Removed and unbuilt backends

Three more backends were built, live-tested, and removed: Portal window
sharing, X11, and a desktop game-window crop. The reason is complexity
and maintenance overhead, not one failure.

- **Portal (ScreenCast).** `org.freedesktop.portal.ScreenCast` with a window
  source, persist mode 2, and a restore token, read through GStreamer
  (`pipewiresrc`, `videoconvert`, `pngenc`). A scan opened the desktop's sharing
  picker, which offers applications, whole screens, and regions with no hint of
  which one works; on Hyprland only **Windows** did, and **Outputs** was
  rejected even while showing the fullscreen game. In live testing the picker
  kept reopening on later scans despite the restore token, so a scan could not
  count on staying silent mid-mission. Each desktop ships its own portal implementation
  (Hyprland, GNOME, KDE, GTK) with its own picker and persistence rules, and
  only Hyprland was verified. It also added `dbus-next` to the scanner runtime
  and GStreamer PipeWire plugins on the host.
- **X11.** `xprop` and ImageMagick `import` of the active window. It cannot
  work in a Wayland session.
- **Desktop crop.** A compositor screenshot cropped to the game window (`grim`
  with `hyprctl` geometry on Hyprland), which needs one implementation per
  compositor.

Desktop capture paths read the compositor's output. With HDR on, those frames
arrive oversaturated, and matching degraded most for stratagems dimmed by a
cooldown timer. Gamescope and Steam screenshots capture the game's own rendering
without that shift, and match better.

Screenshot instead reuses the capture tool the user already has: Steam F12 by
default, or any hotkey or script that saves the game window. The desktop owns
permission and UX once, and the plugin only watches a folder. Gamescope needs no
interaction at all.

The Screenshot portal (`org.freedesktop.portal.Screenshot` with
`interactive=false`) was not built. It shares the per-desktop implementation and
first-grant concerns above, and whether it returns the fullscreen game with
correct HDR colors is unverified. A ScreenCast live watch, streaming a few
frames per second cropped to the stratagem list, remains a possible opt-in
addition.

## Recognition and uncertainty

Capture-quality checks reject uniform frames and the known Steam false-color
pattern. Offline `--image` replay bypasses capture-quality checks, so those
checks have separate tests.

`scan_game.py` resolves `auto` to a mode and bounds recognition parallelism to 1–32
workers, default 2. Capture and input remain serialized. Each mode is a declared
`ScanMode`: one recognize function that returns rows and layout, plus the report
warning. `detect` checks cancellation once at entry and the work deadline before
and after recognition; mission fallbacks also check both between rows.
`selection_layout.py` locates one left-player Ready bar and derives tile geometry
from it. If no yellow bar survives, a complete pale panel edge can provide scale
and position; competing/clipped edges and missing occupied tiles reject recovery.
Automatic selection requires at least two occupied top tiles.
`layout/geometry.py` rescales the tile area above that bar to the calibrated
840-pixel Ready-bar width for matching; reports keep source-pixel boxes. It holds
that transform, the calibration base `selection_layout.py` imports, and
`normalized_band`, which expresses the Ready-bar box as resolution-independent
fractions for the report; the two `*_layout.py` modules analyze pixels and stay
beside the matchers.

`mission_layout.py` finds one observed border track in the supported HUD region
and validates row cadence and square borders. Obscured cooldown rows may be
recovered from matching finite side strokes; spacing alone never creates a row.
Closed menus and competing tracks abstain.

`stratagem_detection.py` combines catalog templates, correlation, silhouettes,
color, curated game references, and normalized/colorless retries. Shared shapes
require distinguishing detail. A near-exact normalized glyph match also needs
independent overlap and companion-component support. Blank components and
conflicting decisions remain unresolved.

`recognize/` names what the matchers share. `constants` holds tile geometry and
twelve frame conventions that stay separate because merging equal-looking values
changes results. `Tile` crops a mission row once and caches its frame
category; it reads that category through `icon_normalization`, so it sits above
normalization, and a `Tile` is never read from two threads. `match_result`
declares the ranking keys results carry; every ranking a cached result stores
must be declared there, or a warm cache returns it unvalidated.

Decoded captures enter recognition as Pillow RGB. OpenCV-loaded assets convert
BGR to RGB at that boundary. Artwork classification uses Pillow HSV hue 0-255;
recognition uses OpenCV HSV hue 0-179. The user-facing `blue` filter and matcher
`cyan` category remain separate calibrated vocabularies.

`mission_fallbacks.py` lets unresolved, non-conflicted mission rows use
conservative English-name OCR, then complete arrow-sequence matching. A
sequence must have consistent spacing and directional margins and identify
exactly one catalog entry. These fallbacks share an eight-second budget and
obey scan cancellation and deadlines.

`ScanSession` keeps uncertainty explicit. Unknown observations are not guessed
into IDs; prior assignments can remain unconfirmed and still execute when tapped.
A question mark on an empty slot represents a partial scan, not unused capacity.
Overflow alone does not make a scan partial. Color filters constrain assignment;
press/release guards protect the displayed binding.

## State, cache, and diagnostics

Session tokens are monotonic and only the active token may complete a scan.
Revisions protect queued UI work and pressed actions. Cancellation invalidates
work without clearing assignments unless the user explicitly clears them.

`scan-state/` documents use schema version 1, retain recognition evidence, and
validate their stored context and catalog IDs. Saved sequences are informational;
execution always uses the current catalog. Writes flush and fsync a private file
before atomic replacement. Interrupted scans restore as idle. Invalid files are
preserved, and persistence failure does not block safe in-memory operation.
State/page readers require regular files, use nonblocking no-follow opens, and
bound JSON bytes and depth.

`IconTemplates` shares prepared assets within a scanner process. The disposable
SQLite recognition cache (`scanner/recognition_cache.py`) stores trusted exact
matches, keyed by consumed image features, candidate/context data, and a
fingerprint of matcher code, catalog, references, and library versions.
Read/parse/storage failures become misses.
The fingerprint lists the matcher modules that turn hashed inputs into stored
results: `stratagem_detection`, `icon_normalization`, `mission_references`,
`colorless_icons`, `mission_layout`, the shared tile crop in `recognize/tile`, the
declared rankings in `recognize/match_result`, and the calibrated geometry in
`recognize/constants`. `selection_layout.py` and `layout/geometry.py` run before
keying, so their output reaches the key as hashed pixels and boxes, and they are
not listed; the calibrated values `selection_layout.py` reads from
`recognize/constants` are covered because that file is. Renaming a listed file
without updating `scanner_cache` silently disables the cache.

Routine scans retain no diagnostic screenshots. Explicit debug output is private
user data. Runner-managed diagnostics prune eligible completed runs while
preserving live or unverifiably owned runs. Host jobs remain when process cleanup
is unconfirmed. Flatpak Gamescope also retains file-cleanup recovery records;
Screenshot cleanup retains or restores uncertain files without a durable record.

## Cancellation, containment, and limits

The input lock covers setup, capture, recognition, and cleanup, excluding ordinary
key injection throughout. `ScanOperation` binds one finalizer at construction and
invokes it once. `scan_lifecycle.py` retains resource policy through preparation,
preflight continuation, worker execution, and GTK completion. GTK completion never
releases process ownership. Shutdown cancels work, disconnects supported hooks,
and makes queued callbacks inert; its bounded join can report unfinished cleanup.

The scanner runs in an owned process group with concurrent bounded stdout/stderr
reads. Timeout, cancellation, overflow, and natural exit all require group
cleanup. After TERM/KILL grace periods, uncertain process state retains ownership
rather than reporting completion.

Host commands use `flatpak-spawn --host` under Flatpak and `/usr/bin/setsid`
natively. The guard, command, watchdog, and ordinary descendants share a process
group. Durable `ready`/`result` markers record lifecycle, but a separate bounded
host query must prove no non-zombie members remain before `complete` is recorded.
A submitted command without `ready` waits for its absolute shared-kernel deadline
and then requires nonce-absence proof. Late commands refuse to launch after that
deadline. Failed queries can retain the input lock beyond the scan timeout.
Trusted scripts must not daemonize; deliberately detached descendants fall outside
this process-group boundary.

| Resource | Limit |
| --- | --- |
| Scanner / work budget | 120 / 110 seconds |
| Report stdout / stderr | 4 MiB / 256 KiB |
| Report rows / text field | 32 / 4,096 characters |
| Image bytes / dimension / pixels | 64 MiB / 16,384 / 40 million |
| Saved JSON bytes / nesting | 1 MiB / 64 levels |
| Screenshot directory entries | 4,096 |
| Recognition cache | 512 entries, 64 KiB each |
| Completed managed diagnostics | 20 runs, 512 MiB |

## Runtime and validation

Native scanning uses the feature-owned `automatic_stratagems/.venv` and pinned
feature requirements. Flatpak uses its sandbox interpreter and a verified GNOME 50,
x86_64, CPython 3.13 profile: sandbox numerical/input libraries plus locked
Tesseract, Leptonica, and English data. `setup-runtime` explicitly builds, checks,
activates, and rolls back profiles under ignored `automatic_stratagems/runtime/`.
Real import/OCR checks validate setup. Only child environment variables change;
plugin startup and scanning never install dependencies or use a host recognition
environment.

`provision.runtime_install.ensure_scanner_runtime` is the single preparation path. It
verifies the resolved runtime first, installs once when that fails, verifies the
result, and records `state`, `profile`, `error`, and `updated_at` in ignored
`automatic_stratagems/runtime/setup-status.json`. It prepares nothing while the
feature setting is off, reports failures instead of raising them, and is called
from three places: `__install__.py` (StreamController's store install and update
hook, using the plugin's own directory layout to find its settings and unwrapping
StreamController's `file-version` 2.0 envelope; flat pre-2.0 files are read as-is), the settings
switch and its **Scanner setup** row, and the scan worker thread, inside the scan
operation, on any `ScanSetupError` (including a missing capture command). That
last caller holds the shared input lock until preparation finishes, so no scan
can start meanwhile. It has no cancel event, a native pip install may run for
1800 seconds, and the bounded shutdown join does not wait for it. The scan
prepares the runtime in place of scanning and asks for another
scan, so scanning still never installs. Installation is idempotent: Flatpak
profiles are content-addressed and reused, and the native environment is rebuilt
whenever it cannot be verified. The plugin imports `runtime_install`
(`settings_rows.py`, `runtime_preparation.py`, `scan_actions.py`), which imports
`build` lazily, only when a Flatpak install runs. The download and archive code
lives in `build.py`.

The settings switch and **Scanner setup** row prepare the runtime on a daemon
thread that shutdown deliberately does not join, because a native install can run
for up to 30 minutes. If StreamController exits mid-install, the pip child is not
owned and may finish on its own, a Flatpak staging directory can remain under
`runtime/staging/`, and the status record keeps the previous attempt. The next
preparation re-verifies and rebuilds; activation is atomic, so a half-built
profile is never selected.

Tests cover sessions/actions, persistence, generated pages, stale completion,
input ownership, process cleanup, recognition, and negative screenshots. The
[README test commands](../README.md#replay-diagnostics-and-tests) separate unit
checks, offline replays, translated fixtures, isolated Flatpak installation/OCR,
and opt-in real host transport. Dummy commands and saved images do not establish
live capture, hardware input, or desktop compatibility.

The calibrated profile is English at 5120×2160 with bounded geometry transforms.
Fixtures include multiplayer selection, damage/cooldown overlays, washed-out
frames, shared glyphs, and closed-menu negatives. All committed screenshots are
calibration/regression inputs. Independent held-out captures are still needed for
wider accuracy claims. Flatpak Steam/Gamescope live compatibility remains
unverified, and low-memory hosts are not validated.

### Offline fixture evaluation

`fixture_evaluation.py` prepares and validates manifests, then runs serial owned
scanner processes with `--image --mode auto --no-cache`. Results record mode,
status, ordered IDs, timing, and errors. The default is one recognition worker.

External manifests contain `schema_version: 1` and `cases`. Each case provides a
relative non-symlink PNG/JPEG path, SHA-256, dimensions, split, expected mode and
ordered IDs, and capture provenance. Use `null` for an occupied unknown and `[]`
for no detections. Provenance identifies capture/backend/platform, game build,
language, resolution, HDR, player count, HUD scale, and safe area. Held-out cases
require concrete metadata and timezone-aware timestamps; calibration may record
unknown metadata. Rights/privacy fields do not authorize publication.

Validation rejects changed bytes, invalid labels, duplicate paths, and known
cross-split capture overlap. Mixed manifests require an explicit split. Manifest JSON is
bounded to 1 MiB and 256 cases; discovery to 4,096 entries and eight levels.
Private outputs are atomic and never overwrite an existing result. Capture and
label independence remains a human requirement, not something hashes can prove.
