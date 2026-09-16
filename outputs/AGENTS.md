# Migration rules

- Treat `C:\Saintcon\amaze-runner\state` as read-only.
- Never import legacy lifecycle state (`RUNNING`, locks, STOP, supervisor/run files) as active state.
- Preserve source hashes, provenance, conflicts, and exclusions in the destination database.
- Do not coerce incompatible coordinate schemas or invent compass-use records.
- No live connection is part of the migration command.
