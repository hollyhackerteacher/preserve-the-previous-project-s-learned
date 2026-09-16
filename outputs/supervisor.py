#!/usr/bin/env python3
"""Keep exactly two guarded workers on the shared live state database."""
from __future__ import annotations
import argparse, json, subprocess, sys, time
from pathlib import Path

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', default='outputs/live-state.db')
    ap.add_argument('--host', default='amaze.saintcon.org')
    ap.add_argument('--max-restarts', type=int, default=1000)
    args = ap.parse_args()
    root = Path(__file__).resolve().parents[1]
    logs = root / 'work'
    handles = ('hollyhacker', 'hollyhacker_2')
    slots = {h: None for h in handles}
    files = {h: None for h in handles}
    restarts = {h: 0 for h in handles}
    next_start = {h: 0.0 for h in handles}
    last_log_mtime = {h: 0.0 for h in handles}
    last_progress = {h: time.monotonic() for h in handles}

    def launch(handle: str) -> None:
        log = logs / f'live-{handle}.jsonl'
        f = log.open('a', encoding='utf-8')
        p = subprocess.Popen(
            [sys.executable, '-u', str(root/'outputs'/'explorer.py'), '--db', args.db,
             '--host', args.host, '--handle', handle, '--batch-cap', '16',
             '--max-actions', '100000'],
            stdout=f, stderr=subprocess.STDOUT,
        )
        files[handle] = f
        slots[handle] = p
        last_log_mtime[handle] = log.stat().st_mtime if log.exists() else 0.0
        last_progress[handle] = time.monotonic()

    def stop_tree(p) -> None:
        if p is None or p.poll() is not None:
            return
        if sys.platform == 'win32':
            subprocess.run(['taskkill', '/PID', str(p.pid), '/T', '/F'],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           check=False)
        else:
            p.terminate()

    while any(restarts[h] <= args.max_restarts for h in handles):
        now = time.monotonic()
        for handle in handles:
            p = slots[handle]
            if p is not None and p.poll() is None:
                log = logs / f'live-{handle}.jsonl'
                mtime = log.stat().st_mtime if log.exists() else last_log_mtime[handle]
                if mtime > last_log_mtime[handle]:
                    last_log_mtime[handle] = mtime
                    last_progress[handle] = now
                elif now - last_progress[handle] > 45:
                    # A Python process can survive while its SSH reader is
                    # silent. Recycle only this handle's process tree.
                    stop_tree(p)
            if p is not None and p.poll() is not None:
                files[handle].close()
                files[handle] = None
                slots[handle] = None
                restarts[handle] += 1
                # The challenge may release a dead SSH slot a few seconds later.
                # Keep retrying promptly; a long exponential pause leaves a
                # handle visibly disconnected for minutes.
                next_start[handle] = now + min(5, 1 + restarts[handle] // 10)
            if slots[handle] is None and restarts[handle] <= args.max_restarts and now >= next_start[handle]:
                launch(handle)
        time.sleep(1)

    for f in files.values():
        if f is not None: f.close()

if __name__ == '__main__': main()
