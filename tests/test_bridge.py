#!/usr/bin/env python3
"""Tests for backend/bridge.py — run with: python3 tests/test_bridge.py

Spawns the real bridge as a subprocess and exercises the newline-delimited
JSON protocol over stdio.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _spawn_bridge():
    return subprocess.Popen(
        [sys.executable, os.path.join(ROOT, 'backend', 'bridge.py')],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True, cwd=ROOT,
    )


def _exchange(proc, reqs):
    for r in reqs:
        proc.stdin.write(json.dumps(r) + "\n")
    proc.stdin.flush()
    out_lines = []
    for _ in range(len(reqs)):
        line = proc.stdout.readline()
        assert line, "bridge closed stdout before sending all responses"
        out_lines.append(line.strip())
    return [json.loads(l) for l in out_lines]


def test_bridge_protocol():
    proc = None
    try:
        proc = _spawn_bridge()
        resps = _exchange(proc, [
            {"id": "1", "method": "list_profiles", "params": {}},
            {"id": "2", "method": "bogus", "params": {}},
        ])
        proc.stdin.close()
        proc.wait(timeout=30)
        err = proc.stderr.read()
        assert proc.returncode == 0, f"bridge exited {proc.returncode}: {err}"

        # Response 1: list_profiles -> result (a list)
        assert resps[0]['id'] == '1', resps[0]
        assert 'result' in resps[0], resps[0]
        assert isinstance(resps[0]['result'], list), resps[0]
        # Response 2: unknown method -> clean error, matching id
        assert resps[1]['id'] == '2', resps[1]
        assert 'error' in resps[1], resps[1]
        assert 'Unknown method' in resps[1]['error'], resps[1]
    finally:
        if proc and proc.poll() is None:
            proc.kill()


def test_create_with_host_mount_round_trip():
    """create_profile with host_mount -> listed -> deleted (real state, cleaned up)."""
    proc = None
    host_tmp = tempfile.mkdtemp(prefix='bridge-host-')
    name = f"bridge-test-{os.getpid()}"
    try:
        proc = _spawn_bridge()
        resps = _exchange(proc, [
            {"id": "3", "method": "create_profile",
             "params": {"name": name, "host_mount": host_tmp}},
            {"id": "4", "method": "list_profiles", "params": {}},
            {"id": "5", "method": "delete_profile", "params": {"name": name}},
        ])
        proc.stdin.close()
        proc.wait(timeout=30)
        err = proc.stderr.read()
        assert proc.returncode == 0, f"bridge exited {proc.returncode}: {err}"

        # create -> result carries the host_mount
        assert resps[0]['id'] == '3', resps[0]
        assert 'error' not in resps[0], resps[0]
        assert resps[0]['result']['host_mount'] == host_tmp, resps[0]

        # list -> profile present with host_mount
        assert resps[1]['id'] == '4', resps[1]
        found = [p for p in resps[1]['result'] if p['name'] == name]
        assert len(found) == 1, resps[1]
        assert found[0]['host_mount'] == host_tmp, found

        # delete -> gone
        assert resps[2]['id'] == '5', resps[2]
        assert resps[2]['result']['status'] == 'deleted', resps[2]
    finally:
        if proc and proc.poll() is None:
            proc.kill()
        shutil.rmtree(host_tmp, ignore_errors=True)


def test_update_profile_round_trip():
    """create with proxy -> update host_mount+proxy+timezone -> listed -> deleted."""
    proc = None
    host_tmp = tempfile.mkdtemp(prefix='bridge-host-')
    name = f"bridge-upd-{os.getpid()}"
    try:
        proc = _spawn_bridge()
        # Phase 1: create + update, then verify the signature file BEFORE the
        # delete (which removes the profile directory).
        resps = _exchange(proc, [
            {"id": "6", "method": "create_profile",
             "params": {"name": name, "proxy": "socks5://127.0.0.1:1080"}},
            {"id": "7", "method": "update_profile",
             "params": {"name": name, "host_mount": host_tmp,
                        "proxy": "socks5://127.0.0.1:9050", "timezone": "Asia/Karachi"}},
        ])

        # create -> proxy stored
        assert resps[0]['id'] == '6', resps[0]
        assert 'error' not in resps[0], resps[0]
        assert resps[0]['result']['proxy'] == 'socks5://127.0.0.1:1080', resps[0]

        # update -> both fields changed
        assert resps[1]['id'] == '7', resps[1]
        assert 'error' not in resps[1], resps[1]
        assert resps[1]['result']['host_mount'] == host_tmp, resps[1]
        assert resps[1]['result']['proxy'] == 'socks5://127.0.0.1:9050', resps[1]

        # timezone written into the profile's hardware-signature.json
        sig_path = os.path.join(resps[1]['result']['path'], 'hardware-signature.json')
        assert os.path.isfile(sig_path), sig_path
        with open(sig_path) as f:
            sig = json.load(f)
        assert sig['system']['timezone'] == 'Asia/Karachi', sig

        # Phase 2: list + delete
        resps = _exchange(proc, [
            {"id": "8", "method": "list_profiles", "params": {}},
            {"id": "9", "method": "delete_profile", "params": {"name": name}},
        ])
        proc.stdin.close()
        proc.wait(timeout=30)
        err = proc.stderr.read()
        assert proc.returncode == 0, f"bridge exited {proc.returncode}: {err}"

        # list -> reflects the update
        assert resps[0]['id'] == '8', resps[0]
        found = [p for p in resps[0]['result'] if p['name'] == name]
        assert len(found) == 1, resps[0]
        assert found[0]['host_mount'] == host_tmp, found
        assert found[0]['proxy'] == 'socks5://127.0.0.1:9050', found
        assert found[0]['machine'].get('timezone') == 'Asia/Karachi', found

        # delete -> gone
        assert resps[1]['id'] == '9', resps[1]
        assert resps[1]['result']['status'] == 'deleted', resps[1]
    finally:
        if proc and proc.poll() is None:
            proc.kill()
        shutil.rmtree(host_tmp, ignore_errors=True)


def test_invalid_timezone_rejected():
    proc = None
    name = f"bridge-tz-{os.getpid()}"
    try:
        proc = _spawn_bridge()
        resps = _exchange(proc, [
            {"id": "11", "method": "create_profile", "params": {"name": name}},
            {"id": "12", "method": "update_profile",
             "params": {"name": name, "timezone": "Not/AZone"}},
            {"id": "13", "method": "delete_profile", "params": {"name": name}},
        ])
        proc.stdin.close()
        proc.wait(timeout=30)
        err = proc.stderr.read()
        assert proc.returncode == 0, f"bridge exited {proc.returncode}: {err}"

        assert resps[0]['id'] == '11', resps[0]
        assert 'error' not in resps[0], resps[0]
        assert resps[1]['id'] == '12', resps[1]
        assert 'error' in resps[1], resps[1]
        assert 'Invalid timezone' in resps[1]['error'], resps[1]
        assert resps[2]['id'] == '13', resps[2]
        assert resps[2]['result']['status'] == 'deleted', resps[2]
    finally:
        if proc and proc.poll() is None:
            proc.kill()


def test_proxy_store_round_trip():
    """add_proxy -> list_proxies -> delete_proxy (real store, cleaned up)."""
    proc = None
    name = f"bridge-proxy-{os.getpid()}"
    try:
        proc = _spawn_bridge()
        resps = _exchange(proc, [
            {"id": "20", "method": "add_proxy",
             "params": {"name": name, "url": "socks5://127.0.0.1:19999"}},
            {"id": "21", "method": "list_proxies", "params": {}},
            {"id": "22", "method": "delete_proxy", "params": {"name": name}},
        ])
        proc.stdin.close()
        proc.wait(timeout=30)
        err = proc.stderr.read()
        assert proc.returncode == 0, f"bridge exited {proc.returncode}: {err}"

        assert resps[0]['id'] == '20', resps[0]
        assert 'error' not in resps[0], resps[0]
        assert resps[0]['result']['url'] == 'socks5://127.0.0.1:19999', resps[0]

        assert resps[1]['id'] == '21', resps[1]
        found = [p for p in resps[1]['result'] if p['name'] == name]
        assert len(found) == 1, resps[1]

        assert resps[2]['id'] == '22', resps[2]
        assert resps[2]['result']['status'] == 'deleted', resps[2]
    finally:
        if proc and proc.poll() is None:
            proc.kill()


def test_invalid_proxy_rejected():
    proc = None
    name = f"bridge-bad-{os.getpid()}"
    try:
        proc = _spawn_bridge()
        resps = _exchange(proc, [
            {"id": "10", "method": "create_profile",
             "params": {"name": name, "proxy": "not-a-proxy"}},
        ])
        proc.stdin.close()
        proc.wait(timeout=30)
        err = proc.stderr.read()
        assert proc.returncode == 0, f"bridge exited {proc.returncode}: {err}"

        assert resps[0]['id'] == '10', resps[0]
        assert 'error' in resps[0], resps[0]
        assert 'Invalid proxy' in resps[0]['error'], resps[0]
    finally:
        if proc and proc.poll() is None:
            proc.kill()


def main():
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith('test_') and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS {name}")
        except Exception:
            failed += 1
            print(f"FAIL {name}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)


if __name__ == '__main__':
    main()