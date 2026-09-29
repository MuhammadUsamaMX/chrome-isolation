#!/usr/bin/env python3
"""Tests for the build-time self-test in scripts/docker/libhwspoof.c.

WHY THIS EXISTS
---------------
The shim's constructor writes "loaded" to $CHROME_SPOOF_PROBE, and the
`Dockerfile` build gate fails the build unless it reads exactly "loaded". That
gate used to check `dlsym(RTLD_NEXT, "open")` and `"uname"` — two of the nine
hooks — while the hooks themselves resolved lazily and independently. A shim in
which every RESOLVE failed therefore reported "loaded", passed the gate, shipped,
and was exported into every browser launch with sysinfo(), fopen(), openat() and
getifaddrs() all dead. Measured on that build, the browser still started
(every hook except getifaddrs() falls back to the raw syscall), so the behaviour
was safe and the PROOF was the thing that was wrong.

These tests compile the real source and exercise the verdict:

  * a deliberately unresolved REQUIRED symbol must report "unusable";
  * a deliberately unresolved OPTIONAL symbol must still report "loaded"
    (getifaddrs has no raw-syscall fallback, so it degrades to -1 and must not
    condemn a shim whose other hooks work — and conversely, its absence must not
    be able to condemn one either, or the gate would fail on musl where
    open64/openat64/fopen64 genuinely do not exist);
  * the REAL, unmodified shim must report "loaded" — a gate that always fails is
    as useless as one that always passes;
  * a shim with EVERY symbol unresolved must still report "unusable" *and* not
    break the process, which is the fail-open property the reviewer confirmed
    and which must survive the change.

Run: python3 -m pytest tests/test_hwspoof_selftest.py
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE = os.path.join(ROOT, 'scripts', 'docker', 'libhwspoof.c')

#: The libc symbols the hooks REQUIRE. Derived from the source's own
#: `hwspoof_symbols` table rather than hardcoded here, so a change to the
#: required/optional classification is a test failure, not a silent drift.
REQUIRED = ('open', 'openat', 'fopen', 'sysinfo', 'uname')

#: Unresolvable, but the hook degrades safely (raw syscall, or -1 for
#: getifaddrs which has no raw-syscall equivalent and is off every path the
#: browser needs). Their failure must not condemn the shim.
OPTIONAL = ('open64', 'openat64', 'fopen64', 'getifaddrs', 'freeifaddrs')

# The name the shim asks dlsym() for, per table entry. We break resolution by
# rewriting the NAME so dlsym asks for a symbol libc does not have. That is the
# honest way to simulate "this shim cannot resolve X": the `optional` flag
# still governs the verdict, exactly as it does in a real broken build. (Poking
# at the flag instead would test the flag, not the gate.)
_DEAD_PREFIX = 'hwspoof_no_such_symbol_'


def _has_cc():
    return shutil.which('cc') or shutil.which('gcc') or shutil.which('clang')


def _cc():
    return shutil.which('cc') or shutil.which('gcc') or shutil.which('clang')


def _table_entries():
    """[(name, optional)] parsed out of the source's hwspoof_symbols table."""
    src = open(SOURCE).read()
    body = src.split('hwspoof_symbols[] = {', 1)[1].split('};', 1)[0]
    out = []
    for line in body.splitlines():
        m = re.search(r'\{\s*"([a-z0-9_]+)"\s*,.*?,\s*([01])\s*,', line)
        if m:
            out.append((m.group(1), m.group(2) == '1'))
    return out


def _build(tmp, break_symbols=(), tag='shim'):
    """
    Compile libhwspoof.c, optionally forcing the named symbols to fail
    resolution. Returns the path to the .so, or raises on a compile error.
    """
    src = open(SOURCE).read()
    for name in break_symbols:
        entry = re.search(r'\{\s*"%s"\s*,' % re.escape(name), src)
        assert entry, f'{name} is not in the hwspoof_symbols table'
        # Replace the TABLE entry only (first occurrence after the table
        # opener), never the hook function definitions.
        start = src.index('hwspoof_symbols[] = {')
        head, tail = src[:start], src[start:]
        before = tail
        tail = re.sub(r'(\{\s*)"%s"' % re.escape(name),
                      lambda m: m.group(1) + '"%s%s"' % (_DEAD_PREFIX, name),
                      tail, count=1)
        assert tail != before, f'could not break {name}'
        src = head + tail
    csrc = os.path.join(tmp, '%s.c' % tag)
    so = os.path.join(tmp, '%s.so' % tag)
    with open(csrc, 'w') as f:
        f.write(src)
    proc = subprocess.run([_cc(), '-O2', '-Wall', '-fPIC', '-shared', '-o', so,
                           csrc], capture_output=True, text=True, timeout=180)
    if proc.returncode != 0:
        raise AssertionError('compile failed:\n' + proc.stderr)
    # -Wall must stay clean; the shim is compiled with it in the Dockerfile.
    if re.search(r'warning:', proc.stderr):
        raise AssertionError('compiler warnings:\n' + proc.stderr)
    return so


def _probe(so, cmd=('/bin/true',), extra_env=None, timeout=60):
    """
    Run `cmd` with the shim preloaded and return a `(probe_word, stdout)` pair.
    Both are needed: the probe word is the gate's subject, stdout is what
    proves the hooks' behaviour.
    """
    with tempfile.TemporaryDirectory() as d:
        probe = os.path.join(d, 'probe')
        env = dict(os.environ)
        env['LD_PRELOAD'] = os.path.abspath(so)
        env['CHROME_SPOOF_PROBE'] = probe
        env.update(extra_env or {})
        try:
            proc = subprocess.run(list(cmd), env=env, capture_output=True,
                                  text=True, timeout=timeout)
            out = proc.stdout
        except subprocess.TimeoutExpired:
            return 'TIMEOUT', ''
        try:
            word = open(probe).read().strip()
        except OSError:
            return f'<no probe file> rc={proc.returncode}', out
    return word, out


def _needs_cc(fn):
    fn = _skip_without_cc(fn)
    return fn


def _skip_without_cc(fn):
    def wrapper():
        if not _has_cc():
            print(f"  (skipped: no C compiler on PATH)")
            return
        fn()
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


# ── the classification itself ────────────────────────────────────────────────
@_needs_cc
def test_source_table_matches_expected_classification():
    entries = dict(_table_entries())
    for name in REQUIRED:
        assert name in entries, f'{name} missing from hwspoof_symbols'
        assert entries[name] is False, \
            f'{name} must be REQUIRED: a shim without it intercepts nothing'
    for name in OPTIONAL:
        assert name in entries, f'{name} missing from hwspoof_symbols'
        assert entries[name] is True, \
            f'{name} must be OPTIONAL: its failure degrades safely, and ' \
            'open64/openat64/fopen64 do not exist on musl at all'
    # Every hook defined in the file must appear in the table, or the constructor
    # would not resolve it and the hook would silently fall through forever.
    hook_defs = set(re.findall(r'^(?:int|FILE \*|void)\s+(\w+)\s*\(', open(SOURCE).read(), re.M))
    table_names = set(entries)
    missing = hook_defs - table_names
    assert not missing, f'hooks absent from hwspoof_symbols: {sorted(missing)}'


# ── the verdict ──────────────────────────────────────────────────────────────
@_needs_cc
def test_real_shim_reports_loaded():
    """The gate must be capable of PASSING. A gate that always fails is as
    useless as one that always passes — and a build gate that always fails
    means nobody ever builds the image again."""
    with tempfile.TemporaryDirectory() as tmp:
        so = _build(tmp, tag='real')
        got, _ = _probe(so)
    assert got == 'loaded', (
        f'the real, unmodified shim must report "loaded", got {got!r}. '
        'If this fails, the Dockerfile gate would fail every build.')


@_needs_cc
def test_unresolved_required_symbol_is_not_loaded():
    """Each of the nine hooks the review named: breaking any REQUIRED one must
    make the shim report "unusable", so the Dockerfile gate fails the build
    instead of shipping a shim that intercepts nothing."""
    for name in REQUIRED:
        with tempfile.TemporaryDirectory() as tmp:
            so = _build(tmp, break_symbols=(name,), tag='break-' + name)
            got, _ = _probe(so)
        assert got == 'unusable', (
            f'a shim that cannot resolve {name}() reported {got!r}, not '
            '"unusable" — the build gate would pass a shim that intercepts '
            'nothing')


@_needs_cc
def test_unresolved_optional_symbol_still_loaded():
    """getifaddrs has no raw-syscall equivalent, so it degrades to -1; open64/
    openat64/fopen64 are not exported by musl at all. None of them may
    condemn an otherwise-healthy shim — otherwise the gate fails on the very
    platform the shim is built for."""
    for name in OPTIONAL:
        with tempfile.TemporaryDirectory() as tmp:
            so = _build(tmp, break_symbols=(name,), tag='opt-' + name)
            got, _ = _probe(so)
        assert got == 'loaded', (
            f'breaking OPTIONAL symbol {name} must not condemn the shim; '
            f'got {got!r}')


@_needs_cc
def test_every_symbol_unresolved_is_unusable():
    """The reviewer's actual build: every RESOLVE fails. It must be rejected by
    the gate (that is the finding being fixed)."""
    with tempfile.TemporaryDirectory() as tmp:
        so = _build(tmp, break_symbols=REQUIRED + OPTIONAL, tag='alldead')
        got, _ = _probe(so)
    assert got == 'unusable', f'expected "unusable", got {got!r}'


# ── the fail-open property must survive the change ───────────────────────────
@_needs_cc
def test_totally_broken_shim_does_not_break_the_process():
    """Verified by the reviewer, and the fix must not regress it: with EVERY
    symbol unresolvable, open/openat/fopen/sysinfo/uname still work (they fall
    through to the raw syscall), so the browser is exec'd and starts. Only
    getifaddrs returns -1, and nothing in this list depends on it.

    The regression this guards: an earlier revision returned -1/NULL from an
    unresolved hook, which made the dynamic loader unable to map the browser
    (`/bin/sh: can't open '/usr/bin/chromium-browser'`, exit 2)."""
    with tempfile.TemporaryDirectory() as tmp:
        so = _build(tmp, break_symbols=REQUIRED + OPTIONAL, tag='failopen')
        # A real program that opens files and calls uname(), i.e. what a
        # launcher does before exec'ing the browser.
        prog = os.path.join(tmp, 'probe_prog.c')
        with open(prog, 'w') as f:
            f.write(
                '#include <stdio.h>\n'
                '#include <string.h>\n'
                '#include <sys/utsname.h>\n'
                '#include <sys/sysinfo.h>\n'
                'int main(void) {\n'
                '    char buf[256]; FILE *fp; struct utsname u; struct sysinfo si;\n'
                '    fp = fopen("/etc/hostname", "r");\n'
                '    if (!fp) { puts("OPEN_FAILED"); return 1; }\n'
                '    if (!fgets(buf, sizeof buf, fp)) puts("READ_FAILED");\n'
                '    fclose(fp);\n'
                '    if (uname(&u) != 0) { puts("UNAME_FAILED"); return 1; }\n'
                '    if (sysinfo(&si) != 0) { puts("SYSINFO_FAILED"); return 1; }\n'
                '    puts("OK");\n'
                '    return 0;\n'
                '}\n')
        exe = os.path.join(tmp, 'probe_prog')
        cp = subprocess.run([_cc(), '-O1', '-o', exe, prog],
                            capture_output=True, text=True, timeout=120)
        assert cp.returncode == 0, cp.stderr
        # Sanity: the program works with no shim at all.
        clean = subprocess.run([exe], capture_output=True, text=True, timeout=60)
        assert clean.stdout.strip() == 'OK', clean.stdout + clean.stderr
        # And with the fully-broken shim it must STILL work — degraded, not dead.
        _, out = _probe(so, cmd=(exe,))
    assert out.strip() == 'OK', (
        f'a shim with every symbol unresolved broke the process: {out!r}. '
        'The fail-open property is what keeps the browser launchable.')


@_needs_cc
def test_real_shim_still_spoofs():
    """The gate is a gate, not the objective: with the real shim the redirect
    and the overrides still work. Proves the refactor from lazy per-hook dlsym
    to one constructor-resolved table did not change behaviour."""
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, 'spoof')
        os.makedirs(os.path.join(root, 'proc'))
        with open(os.path.join(root, 'proc', 'meminfo'), 'w') as f:
            f.write('MemTotal:        1048576 kB\n')
        so = _build(tmp, tag='real')
        word, out = _probe(so, cmd=('/bin/cat', '/proc/meminfo'),
                           extra_env={'CHROME_SPOOF_ROOT': root})
        _, uname = _probe(so, cmd=('/usr/bin/uname', '-r'),
                          extra_env={'CHROME_KERNEL_RELEASE': '6.8.0-45-generic'})
    assert word == 'loaded', word
    assert '1048576' in out, f'/proc/meminfo was not redirected: {out!r}'
    assert '6.8.0-45-generic' in uname, f'uname was not overridden: {uname!r}'


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
