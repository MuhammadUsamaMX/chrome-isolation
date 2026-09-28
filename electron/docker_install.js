'use strict';
/**
 * Docker install recipes (F7) — pure data + validation, no Electron imports.
 *
 * WHY THIS IS A SEPARATE MODULE
 * -----------------------------
 * `electron/main.js` cannot be `require()`d outside Electron (its first line
 * pulls in the `electron` module), which used to make the install recipes
 * untestable. A shell-injection sink that nobody can test regresses, so the
 * recipe table and the username check live here where a plain `node` script can
 * reach them — see `tests/test_docker_install.mjs`.
 *
 * SECURITY
 * --------
 * `process.env.USER` / `process.env.LOGNAME` are attacker-influenceable: login
 * shells, `su` without `-l`, PAM `pam_env`, and a hostile `.bashrc` can all set
 * them. Any value that reaches a `bash -c` string as ROOT (every step below
 * runs under `pkexec`) is arbitrary root code execution.
 *
 * Two independent defences, because either alone has a way to fail:
 *
 *  1. `validateUsername()` accepts only a strict Linux-username allowlist and
 *     throws on anything else, before any process is spawned.
 *  2. No step ever puts the username inside a shell string. `usermod` is always
 *     its own argument-vector step (`['pkexec', ['usermod', '-aG', 'docker', u]]`),
 *     exactly like the `pacman` recipe, so even a validation bypass cannot
 *     reach a shell.
 */

// Linux usernames: start with a letter or underscore, then letters/digits/
// `_`/`-`; a single trailing `$` is allowed for machine accounts. Case
// insensitive because some distributions preserve mixed case.
const USERNAME_RE = /^[a-z_][a-z0-9_-]*\$?$/i;

/** True if `name` is a plausible Unix account name. */
function isValidUsername(name) {
  return typeof name === 'string' && USERNAME_RE.test(name);
}

/**
 * Read the current user name from the environment and validate it.
 *
 * Throws a plain Error (the caller shows it in a dialog) when the variable is
 * missing or is not a legal username — refusing here is the point; there is no
 * "sanitise and continue" path for a value we are about to hand to `usermod`.
 */
function readUsername(env) {
  const source = env || process.env;
  const raw = source.USER || source.LOGNAME;
  if (!raw) {
    throw new Error('Cannot determine the current user name (neither USER nor LOGNAME is set).');
  }
  if (!isValidUsername(raw)) {
    // Deliberately does not echo the value back: it may be attacker data.
    throw new Error(
      'Refusing to continue: the USER/LOGNAME environment variable does not look ' +
      'like a valid Unix user name, so it is not safe to pass to usermod. ' +
      'Log in with a normal account and try again.'
    );
  }
  return raw;
}

/**
 * Best-effort user name for *display only* (dialog text). Never used to build
 * a command line, so an untrusted value is truncated to something harmless
 * rather than throwing in the middle of a warning dialog.
 */
function displayUsername(env) {
  const source = env || process.env;
  const raw = source.USER || source.LOGNAME;
  if (!isValidUsername(raw)) return 'your account';
  return raw;
}

/**
 * Steps that must run as root, per package manager.
 *
 * Every entry is an argument vector `[program, args[]]` — no `sh -c` anywhere
 * near the username. The `bash -c` steps that remain (repo setup / package
 * install) contain only fixed, literal strings with nothing interpolated, so
 * they cannot be steered by the environment.
 */
function buildInstallSteps(pm, user) {
  if (!isValidUsername(user)) {
    throw new Error('buildInstallSteps called with an invalid user name.');
  }
  // Separate step, shared by every recipe: argument-vector, so the username is
  // never parsed by a shell.
  const addToDockerGroup = ['pkexec', ['usermod', '-aG', 'docker', user]];
  const enableDaemon = ['pkexec', ['systemctl', 'enable', '--now', 'docker']];

  switch (pm) {
    case 'pacman':
      return [
        ['pkexec', ['pacman', '-S', '--needed', '--noconfirm', 'docker']],
        enableDaemon,
        addToDockerGroup,
      ];

    case 'apt':
      // get.docker.com handles the apt repo setup and the install. The user
      // name is deliberately NOT part of this string.
      return [
        ['pkexec', ['bash', '-c', 'curl -fsSL https://get.docker.com | sh']],
        enableDaemon,
        addToDockerGroup,
      ];

    case 'dnf-fedora':
      return [
        ['pkexec', ['bash', '-c',
          'dnf config-manager --add-repo https://download.docker.com/linux/fedora/docker-ce.repo && ' +
          'dnf install -y docker-ce docker-ce-cli containerd.io']],
        enableDaemon,
        addToDockerGroup,
      ];

    case 'dnf-rhel':
      return [
        ['pkexec', ['bash', '-c',
          'dnf config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo && ' +
          'dnf install -y docker-ce docker-ce-cli containerd.io']],
        enableDaemon,
        addToDockerGroup,
      ];

    case 'zypper':
      return [
        ['pkexec', ['bash', '-c', 'zypper install -y docker']],
        enableDaemon,
        addToDockerGroup,
      ];

    default:
      throw new Error(`No install recipe for package manager: ${pm}`);
  }
}

const PACKAGE_MANAGERS = ['pacman', 'apt', 'dnf-fedora', 'dnf-rhel', 'zypper'];

module.exports = {
  PACKAGE_MANAGERS,
  USERNAME_RE,
  buildInstallSteps,
  displayUsername,
  isValidUsername,
  readUsername,
};
