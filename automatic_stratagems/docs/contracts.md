# Scanner contracts

## Process boundary

`run_scan(root, backend="auto", workers=2, *, cancel_event=None, debug_dir=None)`
returns a validated JSON report. Cancellation raises
`concurrent.futures.CancelledError`. The runner returns or raises only after
owned child work exits. Ordinary scans retain no diagnostic screenshots.
The root `.venv/bin/python` runs `-m automatic_stratagems.scanner` natively.
Flatpak scans fail preflight: killing the sandbox launcher did not terminate
host descendants that ignored forwarded signals. Cooperative signal forwarding
is insufficient evidence of forced cleanup. Setup is explicit and feature optional.

## Resource and diagnostic limits

- cap scanner stdout at 4 MiB and stderr at 256 KiB while reading both streams
- accept at most 32 report rows and 4,096 characters per bounded text field
- cap encoded images at 64 MiB, dimensions at 16,384 and total pixels at 40 million
- retain Pillow bomb checks and reject size limits before image loading/conversion
- bound scanner work to 120 seconds; preflight has a separate 10-second deadline

The primary profile contains 11 selection slots and 11.1 million pixels; these
caps leave room for report diagnostics and image encoding without unbounded reads.
They are resource ceilings, not additional supported layouts.

`run_scan(debug_dir=...)` uses a marked private cache, limited to 20 owned runs
and 512 MiB through pruning of eligible runs. Live or unverifiably owned runs
are preserved. Runner identity distinguishes abandoned work from live activity.
Direct CLI `--debug-dir` output is user-owned and is not automatically pruned;
existing directories receive a private child. Ordinary scans create neither.

## Input ownership

- setup owns lock finalization until the worker starts successfully
- the worker finalizes after runner cleanup; UI completion never releases
- cancellation and finalization are idempotent, including before process spawn
- timeout, overflow and cancellation terminate and reap owned work
- failed UI scheduling cancels the session without touching GTK in the worker
- token/revision checks reject stale completion; quit rejects future callbacks

The existing whole-scan input exclusion remains: ordinary key actions resume
only after scanner work stops. If the kernel cannot confirm termination, exclusion
stays held; shutdown reports incomplete cleanup after its bounded wait.
Session policy has no process or GTK knowledge.

## Recognition support

The calibration profile is English UI at 5120×2160. Existing panels and their
translations within ±30 source pixels (scaled with the profile) are geometry
regressions, not held-out evidence. Border candidates are observed throughout
that range; competing tracks abstain.
Every annotated unambiguous row and ID must be recovered in that finite set.
Competing anchors and unknown items must abstain. Multiplayer, other aspect
ratios and new HDR pipelines require independent captures before certification.
Mission fallback shares an eight-second budget, further bounded by the scan
deadline. Unresolved rows stay unknown when it expires. The runner terminates
in-flight OCR with the process tree when cancelled.

No live installation, input or gameplay support follows from unit/replay tests.
