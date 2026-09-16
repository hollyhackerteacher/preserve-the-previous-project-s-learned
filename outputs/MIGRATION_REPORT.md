# State migration report

Completed 2026-09-16 with a fresh destination database. The legacy directory
`C:\Saintcon\amaze-runner\state` was read-only; no live SSH connection was
opened and no original file was overwritten.

Destination: `state.db`

## Imported

- 183,283 canonical rooms from the four-coordinate planner schema.
- 392,343 canonical edges; 392,318 verified by reciprocal or cross-planner evidence.
- 7,039 remaining advertised-but-unresolved frontier claims.
- 86 compact verified replay routes, one per discovered ascent origin/layer.
- 646,754 provenance records: 602,008 planner edge claims plus 44,746 legacy direct-observed edge records.
- Legacy database audit totals retained: 15,894 states, 44,746 edges, and 2,946,182 raw observations. The legacy two-coordinate records were not coerced into canonical rooms.

## Conflicts and exclusions

- Conflicts: 0 edge-target conflicts between planner-1 and planner-2.
- Exclusions: 20 records: 19 lifecycle/control artifacts (STOP flags, locks,
  run files, and supervisor files) and one compass-audit notice.
- Compass-use records: 0 imported. Both planner `compass_layers` arrays are
  empty, and the legacy observation text contains no machine-readable compass
  or bearing record. No compass fact was invented.

## Source identity

- `planner-1.json`: `ca17b1d24ffc6671b141824e82fa67392b93d5addbf58f0ca7a6c9f702c2d0a8`
- `planner-2.json`: `33eb5aca7ba065ad8913a9894131513d765a37c7da04a684e54bb2f619c8f1a8`
- `map.db`: `1a245b9ab5a79c4370b47129f8e9d6536b0fee11e07581944154306ecb17921c`

Validation passed with SQLite `PRAGMA integrity_check = ok`. The migration
refuses to overwrite an existing destination.
