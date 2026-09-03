#!/usr/bin/env python3
"""Tests for backend/proxy_store.py — run with: python3 tests/test_proxy_store.py"""
import os
import shutil
import sys
import tempfile
import traceback

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'backend'))

import proxy_store


def _setup():
    tmp = tempfile.mkdtemp(prefix='proxy-store-test-')
    proxy_store.PROXIES_FILE = os.path.join(tmp, 'proxies.json')
    return tmp


def test_add_list_delete():
    tmp = _setup()
    try:
        assert proxy_store.list_proxies() == []
        proxy_store.add_proxy('tunnel-a', 'socks5://127.0.0.1:10080')
        proxy_store.add_proxy('tunnel-b', 'socks5://user:pass@127.0.0.1:10081')
        proxies = proxy_store.list_proxies()
        assert {p['name'] for p in proxies} == {'tunnel-a', 'tunnel-b'}, proxies

        proxy_store.delete_proxy('tunnel-a')
        assert [p['name'] for p in proxy_store.list_proxies()] == ['tunnel-b']

        # deleting an unknown proxy raises
        try:
            proxy_store.delete_proxy('nope')
            assert False, "expected ValueError"
        except ValueError:
            pass
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_validation():
    tmp = _setup()
    try:
        # bad name
        try:
            proxy_store.add_proxy('bad name!', 'socks5://127.0.0.1:1080')
            assert False, "expected ValueError for bad name"
        except ValueError:
            pass
        # bad url
        try:
            proxy_store.add_proxy('ok', 'not-a-proxy')
            assert False, "expected ValueError for bad url"
        except ValueError:
            pass
        # valid forms
        proxy_store.add_proxy('http', 'http://proxy.example.com:8080')
        proxy_store.add_proxy('auth', 'socks5://user:pass@host:1080')
        assert len(proxy_store.list_proxies()) == 2
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_resolve():
    tmp = _setup()
    try:
        proxy_store.add_proxy('tunnel-a', 'socks5://127.0.0.1:10080')
        # saved name -> its URL
        assert proxy_store.resolve('tunnel-a') == 'socks5://127.0.0.1:10080'
        # custom URL -> passed through
        assert proxy_store.resolve('socks5://1.2.3.4:1080') == 'socks5://1.2.3.4:1080'
        # empty -> empty
        assert proxy_store.resolve('') == ''
        assert proxy_store.resolve(None) == ''
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


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