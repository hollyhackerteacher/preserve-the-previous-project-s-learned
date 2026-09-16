#!/usr/bin/env python3
"""Fresh, offline-safe SAINTCON runner foundation and read-only state migrator.

The migration command never opens a legacy SQLite database for writing and never
copies legacy lifecycle/control state into the new active state.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import subprocess
import queue
import threading
import time
from collections import defaultdict, deque
from pathlib import Path

CARDINAL = {"e": (1, 0), "w": (-1, 0), "n": (0, 1), "s": (0, -1)}
REVERSE = {"e": "w", "w": "e", "n": "s", "s": "n", "u": "t", "t": "u"}
SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS migration_runs (run_id TEXT PRIMARY KEY, created_at REAL NOT NULL, source_hashes TEXT NOT NULL, report TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS rooms (room_id TEXT PRIMARY KEY, cube INTEGER NOT NULL, layer INTEGER NOT NULL, x INTEGER NOT NULL, y INTEGER NOT NULL,
                    first_seen REAL, last_seen REAL, provenance TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS edge_evidence (id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, source_file TEXT NOT NULL, source_kind TEXT NOT NULL,
                            source TEXT NOT NULL, action TEXT NOT NULL, target TEXT NOT NULL, observed_at REAL,
                            validation TEXT NOT NULL, reason TEXT, FOREIGN KEY(run_id) REFERENCES migration_runs(run_id));
CREATE TABLE IF NOT EXISTS edges (source TEXT NOT NULL, action TEXT NOT NULL, target TEXT NOT NULL, verified INTEGER NOT NULL,
                    provenance TEXT NOT NULL, first_seen REAL, last_seen REAL, PRIMARY KEY(source, action));
CREATE TABLE IF NOT EXISTS frontier (room_id TEXT NOT NULL, action TEXT NOT NULL, status TEXT NOT NULL, provenance TEXT NOT NULL,
                       PRIMARY KEY(room_id, action));
CREATE TABLE IF NOT EXISTS replay_routes (route_id TEXT PRIMARY KEY, cube INTEGER NOT NULL, layer INTEGER NOT NULL,
                            start_room TEXT NOT NULL, end_room TEXT NOT NULL, actions TEXT NOT NULL,
                            hops INTEGER NOT NULL, verified INTEGER NOT NULL, provenance TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS compass_uses (id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, source_file TEXT NOT NULL, worker TEXT,
                           layer TEXT, origin TEXT, bearing TEXT, status TEXT NOT NULL, observed_at REAL,
                           notes TEXT, FOREIGN KEY(run_id) REFERENCES migration_runs(run_id));
CREATE TABLE IF NOT EXISTS conflicts (id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, kind TEXT NOT NULL, subject TEXT NOT NULL,
                        left_value TEXT, right_value TEXT, sources TEXT NOT NULL, FOREIGN KEY(run_id) REFERENCES migration_runs(run_id));
CREATE TABLE IF NOT EXISTS exclusions (id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, source_file TEXT NOT NULL, record_kind TEXT NOT NULL,
                         subject TEXT, reason TEXT NOT NULL, FOREIGN KEY(run_id) REFERENCES migration_runs(run_id));
CREATE INDEX IF NOT EXISTS edges_target_idx ON edges(target);
CREATE INDEX IF NOT EXISTS frontier_status_idx ON frontier(status);
"""


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def node_parts(node: str):
    parts = node.split(":")
    if len(parts) != 4 or not all(p.lstrip("-").isdigit() for p in parts):
        return None
    return tuple(map(int, parts))


def geometric(source: str, action: str, target: str) -> bool:
    a, b = node_parts(source), node_parts(target)
    if not a or not b:
        return False
    c, l, x, y = a
    c2, l2, x2, y2 = b
    if action in CARDINAL:
        dx, dy = CARDINAL[action]
        return (c, l, x + dx, y + dy) == b
    if action == "u":
        return (c, l + 1, x, y) == b
    # The observed layer-63 to next-cube layer-0 wrap is a valid transition.
    return action == "t" and (c2, l2, x2, y2) == (c + 1, 0, 0, 0) and (l, x, y) == (63, 63, 63)


def route_actions(prev, start, end):
    out = []
    cur = end
    while cur != start:
        parent, action = prev[cur]
        out.append(action)
        cur = parent
    return "".join(reversed(out))


def build_verified_route(db_path: Path, start="0:0:0:0") -> str:
    con = sqlite3.connect(db_path)
    adj = defaultdict(list); asc = {}
    for source, action, target in con.execute("SELECT source,action,target FROM edges WHERE verified=1"):
        adj[source].append((target, action))
        if action == "u": asc[source] = target
    con.close()
    current, result = start, []
    while True:
        q, prev, goal = deque([current]), {current: (None, "")}, None
        while q:
            node = q.popleft()
            if node in asc:
                goal = node; break
            for nxt, action in sorted(adj[node]):
                if nxt not in prev:
                    prev[nxt] = (node, action); q.append(nxt)
        if goal is None: break
        result.append(route_actions(prev, current, goal) + "u")
        current = asc[goal]
    return "".join(result)


def migrate(source_dir: Path, destination: Path) -> dict:
    source_dir = source_dir.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    p1, p2, legacy = [source_dir / n for n in ("planner-1.json", "planner-2.json", "map.db")]
    for p in (p1, p2, legacy):
        if not p.is_file():
            raise FileNotFoundError(p)
    planners = [(p1, json.loads(p1.read_text(encoding="utf-8"))), (p2, json.loads(p2.read_text(encoding="utf-8")))]
    hashes = {p.name: sha256(p) for p in (p1, p2, legacy)}
    con = sqlite3.connect(destination)
    con.executescript(SCHEMA)
    source_hashes = json.dumps(hashes, sort_keys=True)
    existing = con.execute("SELECT report FROM migration_runs WHERE source_hashes=? ORDER BY created_at DESC LIMIT 1",
                           (source_hashes,)).fetchone()
    if existing:
        con.close()
        return json.loads(existing[0])
    run_id = f"migration-{time.time_ns()}"
    report = {"run_id": run_id, "sources": hashes, "rooms": 0, "edges": 0, "verified_edges": 0,
              "replay_routes": 0, "conflicts": 0, "excluded": 0, "legacy_evidence": {}, "compass_records": 0}
    con.execute("INSERT INTO migration_runs VALUES (?,?,?,?)", (run_id, time.time(), source_hashes, "{}"))
    edge_claims = defaultdict(list)
    available_claims = defaultdict(list)
    rooms = set()
    for path, data in planners:
        for room, actions in data.get("available", {}).items():
            rooms.add(room)
            for action in actions:
                available_claims[(room, action)].append(path.name)
        for source, actions in data.get("edges", {}).items():
            rooms.add(source)
            for action, target in actions.items():
                rooms.add(target)
                edge_claims[(source, action)].append((target, path.name))
    claim_set = {(source, action, target)
                 for (source, action), claims in edge_claims.items()
                 for target, _ in claims}
    # Cross-source conflicts are retained, with deterministic first-source choice.
    for (source, action), claims in edge_claims.items():
        targets = sorted({t for t, _ in claims})
        if len(targets) > 1:
            report["conflicts"] += 1
            con.execute("INSERT INTO conflicts VALUES (NULL,?,?,?,?,?,?)", (run_id, "edge_target", f"{source}|{action}",
                        json.dumps(targets), None, json.dumps(sorted(s for _, s in claims))))
        if len(targets) > 1:
            for target, src_file in claims:
                con.execute("INSERT INTO edge_evidence(run_id,source_file,source_kind,source,action,target,observed_at,validation,reason) VALUES (?,?,?,?,?,?,?,?,?)",
                            (run_id, src_file, "planner", source, action, target, None, "conflict", "contradictory target claims; excluded from canonical graph"))
            for target in targets:
                con.execute("INSERT INTO exclusions VALUES (NULL,?,?,?,?,?)", (run_id, "planner-merge", "edge", f"{source}|{action}|{target}", "conflicting edge target"))
            report["excluded"] += len(targets)
            continue
        target = targets[0]
        valid = geometric(source, action, target)
        reciprocal = (target, REVERSE[action], source) in claim_set
        cross_source = len({s for _, s in claims}) == 2
        validation = "verified-reciprocal" if valid and reciprocal else ("verified-cross-source" if valid and cross_source else ("geometric" if valid else "excluded"))
        reason = None if valid else "target does not match action geometry"
        for _, src_file in claims:
            con.execute("INSERT INTO edge_evidence(run_id,source_file,source_kind,source,action,target,observed_at,validation,reason) VALUES (?,?,?,?,?,?,?,?,?)",
                        (run_id, src_file, "planner", source, action, target, None, validation, reason))
        if not valid:
            report["excluded"] += 1
            con.execute("INSERT INTO exclusions VALUES (NULL,?,?,?,?,?)", (run_id, "planner-merge", "edge", f"{source}|{action}|{target}", reason))
            continue
        verified = int(reciprocal or cross_source)
        con.execute("INSERT INTO edges VALUES (?,?,?,?,?,?,?) ON CONFLICT(source,action) DO UPDATE SET target=excluded.target,verified=max(verified,excluded.verified),provenance=excluded.provenance",
                    (source, action, target, verified, "+".join(sorted({s for _, s in claims})), None, None))
    for room in rooms:
        p = node_parts(room)
        if p:
            con.execute("INSERT INTO rooms VALUES (?,?,?,?,?,?,?,?)", (room, *p, None, None, "planner-1+planner-2"))
        else:
            report["excluded"] += 1
            con.execute("INSERT INTO exclusions VALUES (NULL,?,?,?,?,?)", (run_id, "planner-merge", "room", room, "invalid room key"))
    for (room, action), sources in available_claims.items():
        if (room, action) not in edge_claims:
            con.execute("INSERT OR IGNORE INTO frontier VALUES (?,?,?,?)", (room, action, "untested", "+".join(sorted(set(sources)))))
    # Offline BFS is only for compact replay artifacts, never frontier selection.
    verified_adj = defaultdict(list)
    for source, action, target, verified, *_ in con.execute("SELECT source,action,target,verified,provenance,first_seen,last_seen FROM edges WHERE verified=1"):
        verified_adj[source].append((target, action))
    ascent = sorted((s, a, t) for (s, a), vals in edge_claims.items() if a == "u" for t, _ in vals if geometric(s, a, t))
    by_layer = defaultdict(list)
    for s, a, t in ascent:
        p = node_parts(s)
        if p: by_layer[(p[0], p[1])].append((s, t))
    for (cube, layer), goals in sorted(by_layer.items()):
        start = f"{cube}:{layer}:0:0"
        if start not in rooms: start = min((r for r in rooms if node_parts(r) and node_parts(r)[:2] == (cube, layer)), default=None)
        if not start: continue
        for idx, (goal, _) in enumerate(sorted(set(goals))):
            q, prev, seen = deque([start]), {start: (None, "")}, {start}
            while q and goal not in seen:
                cur = q.popleft()
                for nxt, act in sorted(verified_adj[cur]):
                    if nxt not in seen:
                        seen.add(nxt); prev[nxt] = (cur, act); q.append(nxt)
            if goal not in prev: continue
            acts = route_actions(prev, start, goal)
            rid = f"cube{cube}-layer{layer}-to-{goal}-{idx}"
            con.execute("INSERT OR IGNORE INTO replay_routes VALUES (?,?,?,?,?,?,?,?,?)", (rid, cube, layer, start, goal, acts, len(acts), 1, "verified reciprocal/cross-source"))
    # Preserve legacy rows as audit evidence; do not coerce 2-D keys into 4-D rooms.
    lc = sqlite3.connect(f"file:///{legacy.as_posix()}?immutable=1", uri=True)
    report["legacy_evidence"] = {"states": lc.execute("select count(*) from states").fetchone()[0], "edges": lc.execute("select count(*) from edges").fetchone()[0], "observations": lc.execute("select count(*) from observations").fetchone()[0], "canonical_imported": 0}
    for row in lc.execute("select source,action,target,session_id,confirmed from edges"):
        con.execute("INSERT INTO edge_evidence(run_id,source_file,source_kind,source,action,target,observed_at,validation,reason) VALUES (?,?,?,?,?,?,?,?,?)",
                    (run_id, "map.db", "direct-observed", row[0], row[1], row[2], row[4], "preserved-audit-only", "legacy 2-D coordinate schema; not canonical"))
    lc.close()
    report["edges"] = con.execute("select count(*) from edges").fetchone()[0]
    report["rooms"] = con.execute("select count(*) from rooms").fetchone()[0]
    report["verified_edges"] = con.execute("select count(*) from edges where verified=1").fetchone()[0]
    report["replay_routes"] = con.execute("select count(*) from replay_routes").fetchone()[0]
    # Explicitly record old lifecycle artifacts as excluded, never active state.
    control = sorted(p.name for p in source_dir.iterdir() if p.name in {"STOP", "STOP-1", "STOP-2", "ADVISOR-STOP", "worker-1.lock", "worker-2.lock"} or p.name.startswith("supervisor-") or p.name.startswith("run"))
    for name in control:
        con.execute("INSERT INTO exclusions VALUES (NULL,?,?,?,?,?)", (run_id, name, "lifecycle", name, "not imported as active state"))
    report["excluded"] += len(control)
    con.execute("INSERT INTO exclusions VALUES (NULL,?,?,?,?,?)", (run_id, "planner-1.json+planner-2.json+map.db", "compass", None,
                "no machine-readable compass-use records found; planner compass_layers are empty and observations contain no compass/bearing text"))
    report["excluded"] += 1
    con.execute("UPDATE migration_runs SET report=? WHERE run_id=?", (json.dumps(report, sort_keys=True), run_id))
    con.commit(); con.close()
    return report


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="command", required=True)
    m = sub.add_parser("migrate")
    m.add_argument("--source", type=Path, default=Path(r"C:\Saintcon\amaze-runner\state"))
    m.add_argument("--destination", type=Path, required=True)
    p = sub.add_parser("probe")
    p.add_argument("--host", default="amaze.saintcon.org")
    p.add_argument("--handle", default="hollyhacker")
    p.add_argument("--actions", default="e", help="raw lowercase actions to send after login")
    p.add_argument("--batch-size", type=int, default=16)
    p.add_argument("--route-db", type=Path, help="build a continuous verified route from this migrated DB")
    p.add_argument("--timeout", type=float, default=8.0)
    args = ap.parse_args()
    if args.command == "migrate":
        print(json.dumps(migrate(args.source, args.destination), indent=2, sort_keys=True))
    elif args.command == "probe":
        if args.batch_size < 1 or args.batch_size > 16:
            raise ValueError("batch-size must be between 1 and 16")
        if args.route_db:
            args.actions = build_verified_route(args.route_db)
        command = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", f"maze@{args.host}"]
        proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        timed_out = False
        chunks = queue.Queue()
        def collect():
            while True:
                chunk = proc.stdout.read1(4096)
                if not chunk: break
                chunks.put(chunk)
        reader = threading.Thread(target=collect, daemon=True); reader.start()
        output = b""; prompts = 0
        def drain_until(target, deadline):
            nonlocal output, prompts
            while prompts < target and time.monotonic() < deadline:
                try: output += chunks.get(timeout=0.1)
                except queue.Empty: pass
                prompts = output.count(b"A-Maze-ing:")
        try:
            proc.stdin.write((args.handle + "\n").encode("ascii")); proc.stdin.flush()
            drain_until(1, time.monotonic() + args.timeout)
            if prompts < 1: raise TimeoutError("login prompt response not observed")
            for offset in range(0, len(args.actions), args.batch_size):
                batch = args.actions[offset:offset + args.batch_size]
                proc.stdin.write(batch.encode("ascii")); proc.stdin.flush()
                drain_until(prompts + len(batch), time.monotonic() + args.timeout)
                if prompts < offset + len(batch) + 1:
                    raise TimeoutError(f"incomplete response after action offset {offset}")
            proc.stdin.close(); proc.wait(timeout=args.timeout)
            reader.join(timeout=1)
            while True:
                try: output += chunks.get_nowait()
                except queue.Empty: break
        except subprocess.TimeoutExpired:
            timed_out = True
            proc.kill(); proc.wait(); reader.join(timeout=1)
        except (TimeoutError, OSError) as exc:
            timed_out = isinstance(exc, TimeoutError)
            proc.kill(); proc.wait(); reader.join(timeout=1)
            while True:
                try: output += chunks.get_nowait()
                except queue.Empty: break
            output += ("\nRUNNER_ERROR: " + str(exc)).encode()
        print(json.dumps({"host": args.host, "handle": args.handle, "returncode": proc.returncode,
                          "timed_out": timed_out, "raw": output.decode("utf-8", "replace")}, indent=2))


if __name__ == "__main__":
    main()
