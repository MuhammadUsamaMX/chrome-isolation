"""
Host share resolution — T1 (F1) fix.

SECURITY BACKGROUND
-------------------
`_build_volumes()` used to bind-mount the user's ENTIRE home directory
(`os.path.expanduser('~')`) read-only at /home/chrome/host whenever a profile
had no explicit `host_mount`. Every profile shipped with `host_mount: ""`, so
each container could read `~/.ssh`, `~/.gnupg`, KWallet/keyring files, dotfiles
and every other browser profile on the box. Read-only blocks writes; it does
nothing to stop exfiltration over the network from a compromised page.

BEHAVIOUR CHANGE (backward compatible on purpose)
-------------------------------------------------
`host_mount == ""` now means: "mount a safe, empty, per-profile share
directory", NOT "mount my home directory". The value stored in profiles.json
is left exactly as-is — we do not rewrite the registry — so profiles created
before this fix silently become safe instead of quietly leaking.

An explicit, non-empty `host_mount` is still honoured: that is a deliberate
user choice. It is, however, validated (see `validate_host_mount`), and the
GUARANTEE is narrower than "any explicit path is fine":

WHAT CAN AND CANNOT BE SHARED EXPLICITLY
----------------------------------------
  ALLOWED   a dedicated directory, and a *subfolder* of your own home
            (e.g. ~/share) — nothing in the home folder itself is exempt.
  REFUSED   the home directory itself, however it is spelled: `~`, `~/`,
            `$HOME`, the expanded absolute path, or `~/.gnupg/..`.
            `/home` is in `_FORBIDDEN_ROOTS` only as an exact match so that
            subfolders stay shareable; that same exemption is why the home
            directory needed its own explicit check, and it has one
            (`_reject_home_itself`).
  REFUSED   system roots/subtrees and credential stores (~/.ssh, ~/.gnupg,
            keyrings, …) regardless of symlinks or `..` traversal.

Default share path format::

    <APP_DATA_DIR>/host-shares/<profile_name>/
    # e.g. ~/.local/share/chrome-isolation-manager/host-shares/Fiverr-Read/

Each directory is created on demand with mode 0700, so an accidentally-empty
mount is harmless: the container sees an empty directory it cannot write to
and that contains no host secrets.
"""
import os
import stat

from config import APP_DATA_DIR, HOST_SHARES_DIR
from validator import validate_profile_name

# Never-mount roots — rejected as an exact match. `/` is rejected outright as
# required; `/home` is rejected because mounting it would expose *every* user's
# home directory, not just the caller's.
_FORBIDDEN_ROOTS = frozenset({
    '/', '/bin', '/boot', '/dev', '/etc', '/home', '/lib', '/lib32',
    '/lib64', '/libx32', '/opt', '/proc', '/root', '/run', '/sbin',
    '/srv', '/sys', '/usr', '/var',
})

# Roots whose *subtrees* are also refused. `/home` is intentionally absent so
# that a user may deliberately share a subfolder of their own home; the
# sensitive credential stores below are blocked individually, and the home
# directory *itself* is refused explicitly by `_reject_home_itself` (otherwise
# `~`, `~/` and `~/.gnupg/..` would resolve to the whole home dir and re-arm
# the F1 finding).
_FORBIDDEN_SUBTREES = frozenset({
    '/bin', '/boot', '/dev', '/etc', '/lib', '/lib32', '/lib64', '/libx32',
    '/opt', '/proc', '/root', '/run', '/sbin', '/srv', '/sys', '/usr', '/var',
})

# Credential stores that must never be exposed to browser content, even if the
# user (or a symlink) points at them. These hold long-lived secrets.
_SENSITIVE_HOME_SUBPATHS = (
    os.path.join('.ssh'),
    os.path.join('.gnupg'),
    os.path.join('.aws'),
    os.path.join('.azure'),
    os.path.join('.kube'),
    os.path.join('.docker'),
    os.path.join('.pki'),
    os.path.join('.password-store'),
    os.path.join('.config', 'pulse'),          # PulseAudio cookie
    os.path.join('.config', 'kwalletrc'),
    os.path.join('.config', 'kwallet'),
    os.path.join('.local', 'share', 'kwallet'),
    os.path.join('.local', 'share', 'keyrings'),
    os.path.join('.local', 'share', 'kwalletd'),
)


def host_share_dir(profile_name: str) -> str:
    """
    Return the dedicated host-share directory for `profile_name` WITHOUT
    creating it. Format: <APP_DATA_DIR>/host-shares/<profile_name>/
    """
    name = validate_profile_name(profile_name)
    return os.path.join(HOST_SHARES_DIR, name)


def ensure_host_share_dir(profile_name: str) -> str:
    """
    Return the dedicated host-share directory for `profile_name`, creating it
    on demand with mode 0700. This is the SAFE DEFAULT used whenever a profile
    has no explicit host_mount.
    """
    path = host_share_dir(profile_name)
    os.makedirs(path, mode=0o700, exist_ok=True)
    # makedirs() honours the umask and does not fix up an existing directory,
    # so tighten the mode explicitly on every launch.
    try:
        os.chmod(path, stat.S_IRWXU)
    except OSError:
        pass
    return path


def _is_within(path: str, root: str) -> bool:
    return path == root or path.startswith(root + os.sep)


def _home_dir() -> str:
    """The caller's home directory, fully resolved. Computed per call because
    a test (or `su`) can change it between calls."""
    return os.path.realpath(os.path.expanduser('~'))


def _reject_home_itself(path: str) -> None:
    """
    Raise ValueError if `path` IS the user's home directory.

    `_FORBIDDEN_ROOTS` contains `/home` but only as an exact match, so
    `$HOME`, `~`, `~/` and `~/.gnupg/..` all resolve to the home directory and
    would otherwise be accepted — re-arming the original F1 finding with a
    single typed character. Subfolders of home are NOT affected: only the exact
    directory is refused.
    """
    home = _home_dir()
    if path == home:
        raise ValueError(
            f"Refusing to mount your entire home directory ({path}). "
            "Share a specific subfolder inside it instead, e.g. "
            f"{os.path.join(home, 'share')}."
        )


def _reject_sensitive(path: str) -> None:
    """Raise ValueError if `path` is (or lives inside) a sensitive location."""
    home = _home_dir()
    if path == home:
        # Checked here as well as separately so the error is emitted even if a
        # future edit drops the call site of _reject_home_itself().
        raise ValueError(
            f"Refusing to mount your entire home directory ({path}). "
            "Share a specific subfolder inside it instead, e.g. "
            f"{os.path.join(home, 'share')}."
        )
    for sub in _SENSITIVE_HOME_SUBPATHS:
        sensitive = os.path.join(home, sub)
        if _is_within(path, sensitive):
            raise ValueError(
                f"Refusing to mount a credential store: {path}. "
                "Point the host folder at a dedicated share directory instead."
            )


def validate_host_mount(raw: str) -> str:
    """
    Validate and normalize a user-supplied `host_mount`.

    Returns the canonical (realpath-resolved) directory to mount, or `''` when
    the field is empty — `''` means "use the safe per-profile share directory"
    (see module docstring), never "use the home directory".

    Raises ValueError for:
      * non-string input
      * forbidden roots (`/`, `/etc`, `/home`, `/usr`, …)
      * the user's home directory itself, in any spelling (`~`, `~/`,
        `$HOME`, `~/.gnupg/..`) — subfolders of home remain allowed
      * credential stores (~/.ssh, ~/.gnupg, keyrings, …)
      * path traversal or symlinks that resolve into either of the above
      * anything that is not an existing directory
    """
    if raw is None:
        return ''
    if not isinstance(raw, str):
        raise ValueError("Host folder must be a string path.")

    raw = raw.strip()
    # Empty means "safe per-profile share dir", NOT "my home directory".
    if not raw:
        return ''

    expanded = os.path.expanduser(raw)
    if not os.path.isabs(expanded):
        expanded = os.path.abspath(expanded)

    # realpath() collapses `..` traversal AND follows symlinks, so a link such
    # as ~/share -> / is caught by the root check below.
    resolved = os.path.realpath(expanded)

    if resolved in _FORBIDDEN_ROOTS:
        raise ValueError(
            f"Refusing to mount system location: {resolved}. "
            "Choose a dedicated folder to share with the container."
        )
    # Anything underneath a forbidden subtree is just as bad — e.g. `/etc/ssh`,
    # `/usr/share/doc`, or a symlink such as `/bin` -> `/usr/bin`.
    for forbidden in _FORBIDDEN_SUBTREES:
        if _is_within(resolved, forbidden):
            raise ValueError(
                f"Refusing to mount system location: {resolved}. "
                "Choose a dedicated folder to share with the container."
            )
    # The home directory itself is never shareable, however it is spelled.
    # (`/home` is exact-match-only in _FORBIDDEN_ROOTS, so `~`, `~/` and
    # `~/.gnupg/..` would all slip past the checks above.)
    _reject_home_itself(resolved)
    _reject_sensitive(resolved)

    if not os.path.isdir(resolved):
        raise ValueError(f"Host folder not found: {resolved}")

    return resolved


def resolve_host_mount(profile_name: str, configured: str = None) -> str:
    """
    Resolve the directory to bind-mount at /home/chrome/host for a profile.

    - explicit, non-empty `configured`  -> validated user choice (may raise)
    - empty / missing                    -> the safe, empty, 0700 per-profile
                                             share directory (created on demand)

    Never returns the user's home directory.
    """
    if configured is not None and str(configured).strip():
        return validate_host_mount(str(configured))
    return ensure_host_share_dir(profile_name)


def describe_default(profile_name: str) -> str:
    """Human-readable path of the default share dir, for logs and the UI."""
    return host_share_dir(profile_name)


__all__ = [
    'APP_DATA_DIR',
    'describe_default',
    'ensure_host_share_dir',
    'host_share_dir',
    'resolve_host_mount',
    'validate_host_mount',
]
