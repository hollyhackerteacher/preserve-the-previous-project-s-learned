# Fresh SAINTCON A-Maze-ing runner foundation

This workspace contains a new Python migration-safe foundation. Legacy state is
read-only input; control files, locks, STOP flags, and supervisor/run status are
never active state.

Create a new destination once:

```powershell
python work\runner.py migrate --source C:\Saintcon\amaze-runner\state --destination outputs\state.db
```

The command refuses to overwrite an existing destination. It imports validated
four-coordinate planner evidence, records provenance and conflicts, retains the
legacy two-coordinate `map.db` rows as audit evidence, and writes compact
verified replay routes. It performs no network connection.

`explorer.py` contains the frontier-planning core for the live workers. Its BFS
is a cache-fill operation after replanning; it does not select a nearest frontier
on every move. Reservations persist while a worker batches through known
corridors, and an unknown exit is appended only as the final action.
