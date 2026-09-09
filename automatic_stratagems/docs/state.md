# Saved assignments

State files live under the plugin data directory in `scan-state/`, outside the
plugin installation. Each filename hashes deck serial, page path and scan group;
the same context is stored in the file. The action configuration shows its path.

Schema version 1 exports `context`, `updated_at`, `last_scan_at`, `scan_number`,
`status`, `message`, `recognized`, `unknown`, `overflow` and `slots`. Each slot
exports `id`, `name`, `sequence`, `color`, `filter`, `unknown`, `unconfirmed` and
`unconfirmed_since_scan`. The latest usable observation is `last_report`.

Files are atomically replaced. Restoration validates IDs against the current
catalog and executes current catalog sequences, never exported sequences.
Interrupted scans do not restart automatically. Invalid or unsupported files
remain untouched until a new valid save replaces them.

These are assignment snapshots, not live cooldown or game state. External
readers must check timestamps and must not treat `scanning` as permission to
execute. Configuration changes invalidate affected bindings before reuse.

Generated pages live in `temporary-pages/` under the plugin data directory, keyed
by deck serial, source page and scan group. Back retains the page and its saved
assignments; reopening it does not scan. The cache survives app restarts.
Holding Auto Stratagems deletes only its generated page and associated scan state.
Deletion must return an active generated page to its source first. If the source
is missing, keep the page recoverable rather than selecting another destination.
