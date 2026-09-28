#!/usr/bin/env python3
"""Pytest wrapper for the Node-side F7 tests (electron/docker_install.js).

The real assertions live in `tests/test_docker_install.mjs` because the code
under test is JavaScript. This wrapper only runs it, so `python3 -m pytest
tests -q` covers the shell-injection fix too. Skipped, not failed, when there is
no `node` on the box.
"""
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, 'tests', 'test_docker_install.mjs')


def test_docker_install_recipes_have_no_usermod_shell_injection():
    node = shutil.which('node')
    if not node:
        print('node not found — skipping the F7 JavaScript tests')
        return
    proc = subprocess.run([node, SCRIPT], capture_output=True, text=True, cwd=ROOT)
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    assert proc.returncode == 0, 'electron/docker_install.js F7 tests failed'
