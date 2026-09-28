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
SCRIPT = os.path.join(ROOT, 'tests', 'test_bridge_queue.mjs')


def test_bridge_queue_never_hangs_a_queued_request():
    node = shutil.which('node')
    if not node:
        print('node not found — skipping the bridge queue JavaScript tests')
        return
    proc = subprocess.run([node, SCRIPT], capture_output=True, text=True, cwd=ROOT, timeout=300)
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    assert proc.returncode == 0, 'electron/bridge_queue.js tests failed'
