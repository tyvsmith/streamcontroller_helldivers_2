# Automatic stratagems architecture

## Boundaries

Automatic stratagems is an optional subsystem under `automatic_stratagems/`.
`main.py` registers its actions and settings integration. The root plugin keeps
ordinary action execution; the scanner never owns or rewrites those action
definitions. When optional imports, setup, or StreamController integration fail,
the original actions remain usable and the automatic action IDs remain stable.

The implementation has four boundaries:

| Area | Responsibility |
| --- | --- |
| StreamController integration | register actions, expose the feature toggle, track page changes, and isolate compatibility failures |
| Action coordination | resolve contexts and slots, serialize scans, manage sessions, update artwork, and queue UI work |
| Persistence and generated pages | atomically save assignments and own cached page files |
| Scanner process | capture a frame, recognize rows, and return bounded JSON |

## Components

- `scan_actions.py`
  - `ScanCoordinator` owns registered automatic actions, sessions, active scans,
    cache operations, cancellation, and redraws.
  - `ScanStratagems` updates or clears the current page and group.
  - `AutoStratagems` provides the page-opening action. `ScanStratagems` also
    dispatches saved `new_page` settings for compatibility.
  - `AutomaticStratagem` resolves a slot, starts scans for empty slots, and
    delegates assigned execution to the existing plugin path.
  - `TemporaryScanBack` returns to the recorded source page.
- `scan_session.py` is the lock-protected state machine for filters,
  assignments, uncertainty, scan tokens, and revisions. It has no process or UI
  dependencies.
- `scan_state.py` validates and atomically persists session snapshots.
- `temporary_scan_page.py` creates, finds, registers, opens, and deletes
  generated StreamController pages.
- `scan_runner.py` validates setup and owns the scanner subprocess, stream caps,
  timeouts, cancellation, reaping, report validation, and private diagnostics.
- `scanner/` owns capture selection, image bounds, recognition, mission-name
  fallback, report construction, and the command-line interface.
- `visibility.py` adapts the beta.15 action-chooser tree without making the
  optional feature load-bearing.
- `scan_artwork.py` builds static and badged images. `assets/` holds the scan
  action images; `scanner/references/` holds curated game crops.

## Data flow

### Scan the current page

1. The initiating action asks `ScanCoordinator` to start a scan for its exact
   deck, page, and group.
2. The coordinator acquires the shared input lock, resolves current action
   settings and context, and starts a session token.
3. A worker calls `run_scan()` with a cancellation event. The runner starts the
   installed root `.venv/bin/python -m automatic_stratagems.scanner` process.
4. The scanner selects a capture backend, bounds and decodes the image,
   recognizes candidates, and writes one JSON report to stdout.
5. The runner caps both output streams and validates the complete report schema.
6. The worker queues completion on the GTK main loop. Session token, revision,
   current context, action presence, and shutdown checks reject stale work.
7. A valid result updates only that page and group, saves its snapshot, and
   redraws affected actions.

Only the initiating action owns loading and failure presentation. Other scanner,
page, and slot actions retain their normal artwork while the shared result is
applied.

### Create or regenerate a page

Page creation still uses an owned source-session token so page changes, disable,
deletion, and shutdown can invalidate queued completion. Its transient mode
preserves the source session payload. Before opening the generated page, the
coordinator restores that payload without rolling back the monotonic token or
revision, then seeds a separate generated-page session from the report.

Regeneration checks coordinator availability, input ownership, page layout, and
button address before deleting the existing page. A busy scan or invalid page
setup therefore leaves the cache intact. Scanner dependency and backend
preflight runs in the worker after deletion; those failures, capture failures,
and recognition failures leave no replacement. The other buttons' caches and
the source assignments remain unchanged.

## Contexts, slots, and cache identity

An in-memory session context is `(deck controller object, action page path, scan
group)`. Its persisted identity uses the deck serial, absolute page path, and
group. Scans and clears never fan out to another context. Generated pages retain
their source context only for navigation and cache identity.

Slots may be explicit positive integers or `-1` for automatic allocation.
Automatic allocation reserves explicit slots, then orders automatic actions by
row, column, state, and action index from the page topology. Allocation is
recomputed from authoritative page data, so registration order does not assign
different numbers. Release-time context, filter, revision, and displayed binding
checks prevent a changed button from executing an earlier pressed assignment.

Generated-page metadata version 2 keys a cache by deck, actual source page, scan
group, and source button address. The address contains the input type and
identifier, state, and action index. A button therefore reopens the same page
after restart, while two buttons in the same group receive different pages.
Moving a button changes its identity. Version 1 pages lack unambiguous ownership;
they remain registered for navigation and cleanup but are never rebound to a
button.

Back retains a generated page. Normal shutdown unregisters generated pages from
the running app but keeps their files. Explicit deletion removes one owner's
page and saved state after returning an active page to its recorded source.
Missing sources or uncertain ownership preserve the page for recovery. Plugin
uninstall attempts cleanup of owned generated pages and retains failures.

## Session and persistence model

`ScanSession` serializes mutations with a lock. `begin()` returns a monotonic
token; only that token may complete or fail the active scan. Revisions change
when visible bindings change and let queued callbacks and pressed actions reject
stale state. Cancellation invalidates the token but does not erase recognized
assignments unless the user explicitly clears them.

State files live under the plugin data directory in `scan-state/`. Each filename
hashes the deck serial, page path, and group, while the same context is stored in
the document. Schema version 1 contains timestamps, scan status and number,
recognized counts, unknown and overflow evidence, slots, and the latest report.
Slot records include the catalog ID, display metadata, filter, uncertainty, and
an informational saved sequence.

Writes use a private temporary file, flush and file `fsync`, then atomic
replacement. Restoration validates catalog IDs and always executes the current
catalog sequence rather than the saved sequence. Interrupted scans restore as
non-running state and do not restart automatically. Invalid or unsupported files
remain untouched until a later valid save replaces them. Persistence failures do
not make in-memory action handling unsafe.

Snapshots are assignments and recognition evidence, not live cooldown or game
state. `unknown`, `overflow`, and `unconfirmed` remain explicit instead of being
converted to guessed stratagem IDs.

## Concurrency and ownership

A plugin-wide input lock excludes ordinary key injection for the whole scan.
Setup owns finalization until the worker starts; the worker releases ownership
only after `run_scan()` has completed its subprocess cleanup. UI completion does
not release the input lock. Cancellation and finalization are idempotent, and a
pre-set cancellation event prevents process creation.

The runner starts the native scanner in its own process group. Timeout,
cancellation, and output overflow trigger termination; natural exit is also
followed by owned-group cleanup. It reads stdout and stderr concurrently to avoid
pipe deadlock, then validates the report after the process group has exited.
The scan timeout is 120 seconds and the scanner receives a 110-second work
budget. Scanner stdout is capped at 4 MiB, stderr at 256 KiB, report rows at 32,
and bounded text fields at 4,096 characters.

After TERM and KILL grace periods, the runner continues waiting until process
group exit is confirmed. The input lock remains held during that wait. Coordinator
shutdown has a bounded join and reports incomplete cleanup if the worker is still
running.

Worker completion is queued to the main loop. Its callback rechecks coordinator
closure, compatibility, action presence, context, session, and token; revisions
and displayed-binding tuples protect action execution. This also covers the
window after a worker has exited but before its queued callback runs. Root
lifecycle and chooser callbacks use weak owners where the host can retain
callbacks. Shutdown cancels all sessions and active operations, disconnects
signals where the host API permits, and makes later callbacks inert.

Flatpak scanning is rejected during preflight. Killing the Flatpak launcher did
not reliably terminate signal-ignoring host descendants, so cooperative signal
forwarding is insufficient for the process ownership contract.

## Capture and recognition

Every live backend requires `hyprctl` to identify a Helldivers window on
Hyprland. The backend-specific paths are:

- Gamescope: `gamescopectl`
- Steam: F12 through `evdev` and `/dev/uinput`, then a Steam screenshot
- Hyprland desktop: `grim` over the identified window geometry

Automatic mode tries Gamescope, Steam F12, then the Hyprland desktop path until
it obtains a usable frame. Recognition uncertainty does not cause capture
fallback. Steam capture leaves the screenshot in Steam's storage.

Decoded input is limited to 64 MiB encoded data, a 16,384-pixel dimension, and
40 million pixels. `scan_game.py` chooses the recognition mode and bounds the
worker pool. In auto mode, `selection_layout.py` first looks for one calibrated
left-player Ready bar and derives selected and equipped boxes. Auto selection
also requires at least two nonempty top selection tiles. Otherwise,
`mission_layout.py` searches the calibrated HUD region for one consistent track
of bordered mission rows.

`stratagem_detection.py` matches those boxes against prepared catalog templates.
It combines detail components, correlation, silhouettes, icon color, curated
game references, normalized retries, and a colorless glyph fallback. Each path
uses its own acceptance rules. Decisive detail evidence can override other
signals; some disagreements trigger normalized retries, while explicitly marked
conflicts remain unknown. Shared weapon shapes and badge components need
dedicated ambiguity fixtures.

Mission rows unresolved by icon evidence may enter `mission_fallbacks.py`.
Conservative English-name OCR runs first, followed by a complete arrow-sequence
match. The sequence must have consistent glyph spacing and direction margins and
must identify exactly one catalog entry. Conflicted icon rows do not use these
fallbacks. The fallback work shares an eight-second budget and stops at the scan
deadline or cancellation event.

The calibrated profile is English at 5120×2160. Repository fixtures cover known
screens and bounded transformations; they are regression inputs, not proof for
other desktop environments, aspect ratios, HUD settings, HDR pipelines,
multiplayer layouts, or live gameplay.

`IconTemplates` shares prepared assets and scales inside one scanner process.
`recognition_cache.py` optionally stores trusted exact matches under
`$XDG_CACHE_HOME/net_jslay_helldivers_2/recognition/recognition.sqlite3`. Keys
cover the consumed pixels or derived features, matcher context, candidate set,
and a fingerprint of matcher code, references, catalog data, and library
versions. Entries are limited to 512 and 64 KiB each. Handled filesystem,
SQLite, and parsing errors are treated as misses. Deeply nested JSON can still
raise an uncaught recursion error; the cache is disposable rather than
authoritative.

Routine scans retain no diagnostic screenshots. Direct CLI `--debug-dir` output
is private user-owned data; runner-managed `--debug-dir` runs are pruned to at
most 20 eligible runs and 512 MiB while live or unverifiably owned runs are
preserved.

## Setup and testing

The installed plugin root must contain `.venv/bin/python` with the pinned NumPy,
OpenCV headless, Pillow, and evdev versions from
`automatic_stratagems/requirements.txt`. The runner does not use a development
checkout or install dependencies. User setup targets Python 3.12 or newer; the
source does not enforce a version independently of the pinned packages and host.

Run all feature tests plus the root key-mapping regression:

```sh
.venv/bin/python automatic_stratagems/tools/check
```

Replay a fixture or private capture without StreamController:

```sh
.venv/bin/python -m automatic_stratagems.scanner \
  --image /path/to/capture.png --json
```

`tests/fixtures/manifest.json` records fixture provenance, expectations, and
known evidence gaps. Unit and replay tests cover policy and recognition without
proving a fresh installation, live capture, physical input, or gameplay. Native
StreamController beta.15 checks have exercised action visibility and saved-report
page lifecycle on a real deck/page manager; those checks did not exercise game
capture or gameplay input.

The manifest's held-out capture list is empty: every committed screenshot is a
calibration or regression input. Independent full-scene selection and mission
captures are required before making wider accuracy claims.

Oversized or deeply nested state documents, recursive cache JSON, and one
end-to-end offline scanner-to-queued-page lifecycle are current regression-test
gaps. Cold recognition can use several GiB of memory; low-memory hosts are not
validated.

## Extension points

- Add a capture backend in `scanner/capture_backends.py`, keep setup checks in
  `scan_runner.py`, and prove cancellation and process cleanup before enabling it.
- Add recognition layouts as explicit calibrated profiles with annotated
  fixtures and abstention cases. Do not widen current geometry claims from
  synthetic transforms alone.
- Add persisted fields by advancing the schema and preserving validation and
  atomic replacement. Keep saved sequences informational.
- Add actions through `main.py` with stable IDs, optional initialization guards,
  chooser visibility handling, and closed-state callback checks.
- Change allocation only through authoritative page topology and retain the
  press/release stale-binding guards.
- Extend generated-page metadata with a new version. Do not infer ownership for
  ambiguous older pages.
