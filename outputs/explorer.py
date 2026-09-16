#!/usr/bin/env python3
"""Deterministic frontier planner for the migrated SAINTCON map.

BFS is used only to fill a route cache after a replan. Movement consumes cached
routes and never performs a graph search per action. Unknown exits are always the
last action in a batch and are returned to the caller for response processing.
"""
from __future__ import annotations

import heapq
import json
import queue
import re
import sqlite3
import subprocess
import threading
import time
from collections import Counter, defaultdict, deque
from dataclasses import dataclass

REVERSE = {"e": "w", "w": "e", "n": "s", "s": "n", "u": "t", "t": "u"}


@dataclass(frozen=True)
class Frontier:
    room: str
    action: str


class FrontierPlanner:
    def __init__(self, db_path, worker, batch_cap=16):
        self.db = sqlite3.connect(db_path, timeout=10, isolation_level=None)
        self.worker = worker
        self.batch_cap = min(16, max(1, batch_cap))
        self.graph = defaultdict(dict)
        self.cache = {}
        self.current = None
        self.held = None
        self.recovery = None
        self.ascents_done = set()
        self.avoid_rooms = set()
        self._load_graph()

    def _load_graph(self):
        self.graph.clear()
        for s, a, t in self.db.execute("SELECT source,action,target FROM edges WHERE provenance NOT LIKE '%server-rejected%'"):
            self.graph[s][a] = t

    def observe_edge(self, source, action, target, verified=False):
        if target == source:
            self.cache.clear()
            self.db.execute("UPDATE frontier SET status='uncertain' WHERE room_id=? AND action=?", (source, action))
            self.held = None
            return
        self.graph[source][action] = target
        self.cache.clear()  # new topology can invalidate every cached route
        self.db.execute("INSERT INTO edges(source,action,target,verified,provenance) VALUES(?,?,?,?,?) "
                        "ON CONFLICT(source,action) DO UPDATE SET target=excluded.target,verified=max(verified,excluded.verified),provenance=excluded.provenance",
                        (source, action, target, int(verified), "live-" + self.worker))
        self.db.execute("UPDATE frontier SET status='resolved' WHERE room_id=? AND action=?", (source, action))

    def observe_menu(self, room, actions):
        for action in actions:
            if action not in self.graph[room]:
                self.db.execute("INSERT OR IGNORE INTO frontier(room_id,action,status,provenance) VALUES(?,?,?,?)",
                                (room, action, "untested", "live-menu-" + self.worker))

    def reserve(self, current=None):
        """Atomically reserve a target; stale reservations are not trusted as active locks."""
        if self.held:
            return self.held
        reachable = None
        if current:
            reachable = {current}; q = deque([current])
            while q:
                node = q.popleft()
                for nxt in self.graph[node].values():
                    if nxt not in reachable:
                        reachable.add(nxt); q.append(nxt)
        rows = self.db.execute("SELECT room_id,action FROM frontier WHERE status='untested' "
                               "ORDER BY CASE WHEN action IN ('u','t') THEN 0 ELSE 1 END, room_id,action").fetchall()
        for room, action in rows:
            if room in self.avoid_rooms:
                continue
            if reachable is not None and room not in reachable:
                continue
            cur = self.db.execute("UPDATE frontier SET status=? WHERE room_id=? AND action=? AND status='untested'",
                                  ("reserved:" + self.worker, room, action))
            if cur.rowcount:
                self.held = Frontier(room, action)
                return self.held
        return None

    def _route(self, start, goal):
        key = (start, goal)
        if key in self.cache:
            return self.cache[key]
        q, prev = deque([start]), {start: (None, None)}
        while q:
            node = q.popleft()
            if node == goal:
                actions = []
                while node != start:
                    node, action = prev[node]
                    actions.append(action)
                route = tuple(reversed(actions)); self.cache[key] = route; return route
            for action, nxt in sorted(self.graph[node].items()):
                if nxt not in prev:
                    prev[nxt] = (node, action); q.append(nxt)
        self.cache[key] = None
        return None

    def plan_batch(self, current):
        """Return known transit plus exactly one unknown action, or None."""
        self.current = current
        while True:
            frontier = self.reserve(current)
            if not frontier:
                if self.recovery is None:
                    reachable = {current}; q = deque([current])
                    while q:
                        node = q.popleft()
                        for nxt in self.graph[node].values():
                            if nxt not in reachable: reachable.add(nxt); q.append(nxt)
                    goals = sorted(s for s, a in ((s, a) for s, acts in self.graph.items() for a in acts)
                                   if a == 'u' and s in reachable and s not in self.ascents_done)
                    if goals: self.recovery = goals[0]
                if self.recovery is None: return None
                route = self._route(current, self.recovery)
                if route is None: self.ascents_done.add(self.recovery); self.recovery = None; continue
                if len(route) >= self.batch_cap:
                    transit = list(route[:self.batch_cap])
                    return {"actions": "".join(transit), "unknown": None, "target": None}
                actions = "".join(route) + "u"
                self.ascents_done.add(self.recovery); self.recovery = None
                return {"actions": actions, "unknown": None, "target": None}
            route = self._route(current, frontier.room)
            if route is not None:
                break
            self.db.execute("UPDATE frontier SET status='untested' WHERE room_id=? AND action=?",
                            (frontier.room, frontier.action))
            self.held = None
            return None
        if self.batch_cap == 1:
            transit = list(route[:1])
        else:
            transit = list(route[: self.batch_cap - 1])
        if len(transit) < len(route) or (self.batch_cap == 1 and route):
            # Keep the reservation; caller will request another cached batch.
            self.db.execute("UPDATE frontier SET status=? WHERE room_id=? AND action=?",
                            ("reserved:" + self.worker, frontier.room, frontier.action))
            return {"actions": "".join(transit), "unknown": None, "target": frontier}
        self.held = None
        return {"actions": "".join(transit) + frontier.action, "unknown": frontier.action, "target": frontier}

    def invalidate_edge(self, source, action):
        self.graph[source].pop(action, None)
        self.cache.clear()
        self.db.execute("UPDATE edges SET verified=0,provenance=provenance||'+server-rejected' WHERE source=? AND action=?",
                        (source, action))

    def close(self):
        self.db.close()


PROMPT = re.compile(r"A-Maze-ing:\s+(?:(?:Cube\s+(\d+):\s+)?)Layer\s+(\d+):\s+\[(\d+),(\d+)\]")
MENU = re.compile(r"A-Maze-ing:.*?Direction\s*\(([^)]*)\):")


def parse_position(text, fallback_cube=0):
    matches = list(PROMPT.finditer(text))
    if not matches:
        matches = list(re.finditer(r"A-Maze-ing:\s+\[(\d+),(\d+)\]", text))
        m = matches[-1] if matches else None
        return f"{fallback_cube}:0:{m.group(1)}:{m.group(2)}" if m else None
    m = matches[-1]; cube = int(m.group(1) or fallback_cube)
    return f"{cube}:{m.group(2)}:{m.group(3)}:{m.group(4)}"


def parse_menu(text):
    matches = list(MENU.finditer(text))
    if not matches: return []
    return [x.strip().lower() for x in matches[-1].group(1).split(',') if x.strip()]


class LiveWorker:
    def __init__(self, db_path, handle, host="amaze.saintcon.org", batch_cap=16, timeout=8):
        self.db_path, self.handle, self.host = db_path, handle, host
        self.batch_cap, self.timeout = min(16, max(1, batch_cap)), timeout
        self.planner = FrontierPlanner(db_path, handle, self.batch_cap)
        self.proc = subprocess.Popen(["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", f"maze@{host}"],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.chunks = queue.Queue(); self.output = b""; self.prompts = 0
        self.search_seen = set()
        self.search_last = None
        self.recent_positions = deque(maxlen=64)
        self.last_telemetry = 0
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        while True:
            chunk = self.proc.stdout.read1(4096)
            if not chunk: return
            self.chunks.put(chunk)

    def _wait_prompts(self, target):
        deadline = time.monotonic() + self.timeout
        while self.prompts < target and time.monotonic() < deadline:
            try: self.output += self.chunks.get(timeout=.1)
            except queue.Empty: pass
            self.prompts = self.output.count(b"A-Maze-ing:")
            if b"You cannot go that way." in self.output[-200:]:
                raise RuntimeError("server rejected an action in the current batch")
        if self.prompts < target:
            raise TimeoutError(f"expected prompt {target}, got {self.prompts}: {self.output[-400:]!r}")

    def run(self, max_actions=100000):
        self.proc.stdin.write((self.handle + "\n").encode()); self.proc.stdin.flush(); self._wait_prompts(1)
        current = parse_position(self.output.decode("utf-8", "replace"))
        if not current: raise RuntimeError("initial position not parsed")
        self.planner.observe_menu(current, parse_menu(self.output.decode("utf-8", "replace")))
        last_menu = parse_menu(self.output.decode("utf-8", "replace"))
        print(json.dumps({"kind": "startup", "handle": self.handle,
                          "server_tail": self.output.decode("utf-8", "replace")[-1200:]},
                         separators=(",", ":")), flush=True)
        print(json.dumps({"kind": "live", "handle": self.handle, "prompts": self.prompts,
                          "position": current}, separators=(",", ":")), flush=True)
        actions_done = 0
        stagnant_batches = 0
        sent = ""; batch_prompts = self.prompts
        error = None
        try:
            while actions_done < max_actions:
                plan = self.planner.plan_batch(current)
                if not plan:
                    # Search the known component instead of circling one edge.
                    # Prefer a branch whose destination this worker has not
                    # visited in search mode; every returned menu can add a new
                    # frontier, after which normal planning resumes.
                    known = self.planner.graph.get(current, {})
                    self.search_seen.add(current)
                    patrol = next((a for a in ('u', 't')
                                   if a in known and known[a] not in self.planner.avoid_rooms), None)
                    if patrol is None:
                        patrol = next((a for a, nxt in sorted(known.items())
                                   if nxt not in self.search_seen
                                   and nxt not in self.planner.avoid_rooms
                                   and a != REVERSE.get(self.search_last)), None)
                        patrol = patrol[0] if patrol else None
                    
                    if patrol is None:
                        patrol = next((a for a in ('e', 'n', 's', 'w')
                                       if a in known
                                       and known[a] not in self.planner.avoid_rooms
                                       and a != REVERSE.get(self.search_last)), None)
                    if patrol is None:
                        patrol = next((a for a in sorted(known)
                                       if known[a] not in self.planner.avoid_rooms), None)
                    if patrol is None:
                        # Quarantine affects frontier selection, not connectivity:
                        # when every exit is quarantined, keep traversing the
                        # least-committal known corridor instead of cleanly
                        # exiting and replaying from the origin.
                        patrol = next((a for a in sorted(known)
                                       if a != REVERSE.get(self.search_last)), None)
                    if patrol is None:
                        patrol = next(iter(sorted(known)), None)
                    if patrol is None:
                        # A newly reached room may have a menu that was split
                        # across prompt chunks; ingest it once more before
                        # probing an advertised exit when the graph is stale.
                        fresh_menu = parse_menu(self.output.decode("utf-8", "replace"))
                        last_menu = fresh_menu or last_menu
                        self.planner.observe_menu(current, last_menu)
                        plan = self.planner.plan_batch(current)
                        if plan:
                            continue
                        probe = next((a for a in last_menu if a not in known), None)
                        if probe is None:
                            probe = next(iter(last_menu), None)
                        if probe is None:
                            time.sleep(0.25)
                            continue
                        plan = {"actions": probe,
                                "unknown": probe if probe not in known else None,
                                "target": Frontier(current, probe) if probe not in known else None}
                    if patrol is not None:
                        self.search_last = patrol
                        plan = {"actions": patrol, "unknown": None, "target": None}
                before = current; sent = plan["actions"]; batch_prompts = self.prompts
                if not sent: raise RuntimeError("empty plan batch")
                self.proc.stdin.write(sent.encode("ascii")); self.proc.stdin.flush()
                self._wait_prompts(self.prompts + len(sent))
                current = parse_position(self.output.decode("utf-8", "replace"), int(before.split(":")[0])) or before
                last_menu = parse_menu(self.output.decode("utf-8", "replace")) or last_menu
                self.planner.observe_menu(current, last_menu)
                if current == before:
                    stagnant_batches += 1
                else:
                    stagnant_batches = 0
                if stagnant_batches >= 8:
                    # A known route can be internally cyclic even while the
                    # server continues returning prompts. Re-probe this room
                    # from its advertised menu instead of feeding the cycle.
                    self.planner.graph[current].clear()
                    self.planner.cache.clear()
                    self.planner.recovery = None
                    self.search_seen.clear()
                    self.search_last = None
                    stagnant_batches = 0
                if plan["unknown"]:
                    self.planner.observe_edge(plan["target"].room, plan["unknown"], current, verified=False)
                actions_done += len(sent)
                self.recent_positions.append(current)
                if len(self.recent_positions) == self.recent_positions.maxlen:
                    counts = Counter(self.recent_positions)
                    repeated = {room for room, count in counts.items() if count >= 6}
                    single_loop = any(count >= 12 for count in counts.values())
                    if (len(repeated) >= 2 and len(counts) <= 12) or (single_loop and len(counts) <= 8):
                        self.planner.avoid_rooms.update(repeated)
                        if self.planner.held:
                            self.planner.db.execute(
                                "UPDATE frontier SET status='untested' WHERE room_id=? AND action=?",
                                (self.planner.held.room, self.planner.held.action))
                            self.planner.held = None
                        self.planner.cache.clear()
                        self.recent_positions.clear()
                        self.search_seen.clear()
                        self.search_last = None
                if self.prompts - self.last_telemetry >= 256:
                    self.last_telemetry = self.prompts
                    print(json.dumps({"kind": "live", "handle": self.handle,
                                      "prompts": self.prompts, "actions": actions_done,
                                      "position": current}, separators=(",", ":")), flush=True)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            latest = parse_position(self.output.decode("utf-8", "replace"), int(current.split(":")[0]))
            confirmed = self.prompts - batch_prompts
            if latest and sent and confirmed < len(sent):
                self.planner.invalidate_edge(latest, sent[confirmed])
            if self.planner.held:
                self.planner.db.execute("UPDATE frontier SET status='uncertain' WHERE room_id=? AND action=?",
                                        (self.planner.held.room, self.planner.held.action))
                self.planner.held = None
        finally:
            try: self.proc.stdin.close(); self.proc.wait(timeout=2)
            except Exception: self.proc.kill()
            self.planner.close()
        return {"handle": self.handle, "actions": actions_done, "position": current,
                "prompts": self.prompts, "error": error,
                "raw_tail": self.output.decode("utf-8", "replace")[-500:]}


def self_test():
    """Small invariant test used by CI/manual checks."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        path = str(__import__('pathlib').Path(d) / "test.db")
        c = sqlite3.connect(path)
        c.executescript("CREATE TABLE edges(source TEXT,action TEXT,target TEXT,verified INTEGER,provenance TEXT,PRIMARY KEY(source,action));"
                        "CREATE TABLE frontier(room_id TEXT,action TEXT,status TEXT,provenance TEXT,PRIMARY KEY(room_id,action));")
        c.executemany("INSERT INTO edges VALUES(?,?,?,?,?)", [("a","e","b",1,"test"),("b","e","c",1,"test")])
        c.execute("INSERT INTO frontier VALUES('c','n','untested','test')"); c.commit(); c.close()
        p = FrontierPlanner(path, "test", 2)
        try:
            first = p.plan_batch("a"); second = p.plan_batch("b")
            assert first["actions"] == "e" and first["unknown"] is None
            assert second == {"actions": "en", "unknown": "n", "target": Frontier("c", "n")}
        finally:
            p.close()


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--db", default="outputs/state.db")
    ap.add_argument("--handle", default="hollyhacker")
    ap.add_argument("--host", default="amaze.saintcon.org")
    ap.add_argument("--max-actions", type=int, default=100000)
    ap.add_argument("--batch-cap", type=int, default=16)
    args = ap.parse_args()
    if args.self_test:
        self_test(); print("explorer self-test: ok")
    else:
        print(json.dumps(LiveWorker(args.db, args.handle, args.host, args.batch_cap).run(args.max_actions), indent=2))
