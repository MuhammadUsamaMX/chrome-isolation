#!/usr/bin/env python3
"""Pytest wrapper for the Node-side F7 tests (electron/docker_install.js).

The real assertions live in `tests/test_docker_install.mjs` because the code
under test is JavaScript. This wrapper only runs it, so `python3 -m pytest
tests -q` covers the shell-injection fix too. Skipped, not failed, when there is
no `node` on the box.

The second test runs `tests/mutation_check_docker_inject.sh`, which re-breaks
the fix in a scratch copy and requires the .mjs suite to notice. Without it the
suite above is a set of assertions nobody has ever seen fail, which is the shape
of claim this project has got wrong before.
"""
import os
import shutil
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, 'tests', 'test_docker_install.mjs')
MUTATION_CHECK = os.path.join(ROOT, 'tests', 'mutation_check_docker_inject.sh')


def test_docker_install_recipes_have_no_usermod_shell_injection():
    node = shutil.which('node')
    if not node:
        print('node not found — skipping the F7 JavaScript tests')
        return
    proc = subprocess.run([node, SCRIPT], capture_output=True, text=True, cwd=ROOT)
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    assert proc.returncode == 0, 'electron/docker_install.js F7 tests failed'


def test_docker_install_tests_are_not_vacuous():
    # The suite is only worth anything if it goes red when the fix is reverted.
    if not shutil.which('node'):
        pytest.skip('node not found — cannot run the mutation check')
    bash = shutil.which('bash')
    if not bash:
        pytest.skip('bash not found — cannot run the mutation check')
    # Checked explicitly, with the reason spelled out, because the alternative
    # is a bare FileNotFoundError traceback from subprocess — or, worse, a
    # skip that reports a vacuous test as having passed. This test PASSES in a
    # working tree where the script happens to exist and FAILS from a clean
    # export where it does not, which is the exact failure mode
    # tests/test_clean_checkout_is_green.py exists to catch; see the note in
    # that file's docstring.
    assert os.path.isfile(MUTATION_CHECK), (
        'the mutation check script is missing: %s\n'
        'This test depends on it, so a clean export of the committed tree '
        'cannot pass while the script is untracked. Add it to git (git add %s) '
        '— it is part of this test, not a local scratch file.'
        % (MUTATION_CHECK, os.path.relpath(MUTATION_CHECK, ROOT)))
    proc = subprocess.run([bash, MUTATION_CHECK], capture_output=True, text=True, cwd=ROOT)
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    # 77 is the script's own "skipped, e.g. no python3" code. Surfaced as a skip
    # so a box that cannot run the check is never reported as having passed it.
    if proc.returncode == 77:
        pytest.skip('mutation check could not run (exit 77); see output above')
    assert proc.returncode == 0, (
        'the docker-install tests did not notice the re-broken module — they are vacuous'
    )

