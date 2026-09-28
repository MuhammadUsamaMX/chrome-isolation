#!/usr/bin/env python3
"""Pytest wrapper for the Node-side bridge queue tests (electron/bridge_queue.js).

The real assertions live in `tests/test_bridge_queue.mjs` because the code under
test is JavaScript. This wrapper only runs it, so `python3 -m pytest tests -q`
covers the R1 bridge-queue fix too. Skipped, not failed, when there is no `node`.
"""
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run_node(script, timeout):
    node = shutil.which('node')
    if not node:
        print(f'node not found — skipping {os.path.basename(script)}')
        return
    proc = subprocess.run([node, script], capture_output=True, text=True, cwd=ROOT, timeout=timeout)
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    assert proc.returncode == 0, f'{os.path.basename(script)} failed'


def test_bridge_queue_never_hangs_a_queued_request():
    _run_node(os.path.join(ROOT, 'tests', 'test_bridge_queue.mjs'), 300)


def test_bridge_queue_end_to_end_against_a_real_child():
    # The unit suite uses fake children; this one drives a real spawn, a real
    # broken stdin and a real SIGKILL, so the shipped code path is exercised.
    _run_node(os.path.join(ROOT, 'tests', 'test_bridge_queue_e2e.mjs'), 300)
