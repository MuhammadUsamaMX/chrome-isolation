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
  ALLOWED   a dedicated directory, including a subfolder of your own home
            (e.g. ~/share, ~/Downloads) that is not itself named below.
            Being *in* the home directory confers nothing: every entry in
            the REFUSED lists below also lives in the home directory. The
            home directory itself is never shareable, however it is spelled.
  REFUSED   the home directory itself: `~`, `~/`, `$HOME`, the expanded
            absolute path, or `~/.gnupg/..`. `/home` is in
            `_FORBIDDEN_ROOTS` only as an exact match so that subfolders stay
            shareable; that same exemption is why the home directory needed
            its own explicit check, and it has one (`_reject_home_itself`).
  REFUSED   system roots/subtrees and credential stores (~/.ssh, ~/.gnupg,
            keyrings, …) regardless of symlinks or `..` traversal.
  REFUSED   this app's own storage, and every real browser profile store we
            can name — see `_app_owned_roots()`. The first is what this app
            exists to keep apart from itself (the other profiles' cookies,
            logins and history, plus `profiles.json`/`proxies.json`); the
            second is the same harm reached through real Chrome's tree. Both
            are one field away in the Edit modal, so "the user chose it" is
            not a reason to allow them.

Default share path format::

    <APP_DATA_DIR>/host-shares/<profile_name>/
    # e.g. ~/.local/share/chrome-isolation-manager/host-shares/Fiverr-Read/

Each directory is created on demand with mode 0700, so an accidentally-empty
mount is harmless: the container sees an empty directory it cannot write to
and that contains no host secrets.
"""
import os
import stat

import paths
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
#
# THE ACCURATE GUARANTEE, because `/home` being exempt is easy to over-read:
# being a subfolder of your home is NECESSARY but not SUFFICIENT for a path to
# be shareable. Three further named deny-lists cut into the home directory:
#   * `_SENSITIVE_HOME_SUBPATHS` — credential stores;
#   * `_BROWSER_PROFILE_DIRS`     — real browsers' own storage;
#   * `_app_owned_roots()`        — this app's own storage (its profile store,
#     its data directory), read from `paths.py` so it cannot drift from where
#     the app actually keeps its data.
# A subfolder of home that is none of those (~/share, ~/Downloads) is allowed.
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

# Real browsers' own storage, relative to the home directory.
#
# WHY THIS IS HERE AND NOT A "NOBODY WOULD DO THAT" ARGUMENT: F1 was opened
# because an *explicit* host mount defaulted to the whole home directory, which
# handed every container every OTHER browser profile on the box. Naming the app's
# own profile store without also naming real Chrome's leaves the original harm
# fully reachable — `~/.config/google-chrome` is one field away in the Edit
# modal and holds the same cookies, logins and history that F1 was about. The
# profile-store relocation commit did not change that; it only moved our copy
# of the data to a fresh name. So both halves are refused.
#
# A store this list does not name is not thereby approved — it is merely not
# recognised. The list cannot be exhaustive (users install browsers into
# arbitrary locations), and the residual risk is a deliberately typed path to
# a browser profile store whose name we do not know. That is a much smaller
# hole than "every browser profile is one field away", and it is the honest
# limit of a name-based check.
_BROWSER_PROFILE_DIRS = (
    # Chrome / Chromium and their channels
    os.path.join('.config', 'google-chrome'),
    os.path.join('.config', 'google-chrome-beta'),
    os.path.join('.config', 'google-chrome-dev'),
    os.path.join('.config', 'google-chrome-unstable'),
    os.path.join('.config', 'google-chrome-for-testing'),
    os.path.join('.config', 'chromium'),
    os.path.join('.config', 'chromium-browser'),
    # Firefox / Gecko family
    os.path.join('.mozilla'),                     # also covers firefox/ and librewolf/
    os.path.join('.librewolf'),
    os.path.join('.thunderbird'),
    os.path.join('.gnome2', 'epiphany'),
    # Other Chromium browsers
    os.path.join('.config', 'BraveSoftware'),
    os.path.join('.config', 'microsoft-edge'),
    os.path.join('.config', 'opera'),
    os.path.join('.config', 'vivaldi'),
    os.path.join('.config', 'com.donutbrowser'),
    # Flatpak installs, whose user data lives under ~/.var/app/<id>
    os.path.join('.var', 'app', 'com.google.Chrome'),
    os.path.join('.var', 'app', 'org.mozilla.firefox'),
    os.path.join('.var', 'app', 'com.brave.Browser'),
    # ~/.config ITSELF, as a subtree. Every Chrome/Chromium/Brave/Edge entry
    # above lives under it, so allowing it would hand out all of them at once
    # and make the entire list above decorative. Concretely,
    # `~/.config/google-chrome/..` realpaths to `~/.config`, so without this
    # entry the google-chrome refusal could be bypassed with one `..`.
    # It is refused as a SUBTREE, so `~/.config/<something-else>` is refused
    # too: an unrecognised app's config may hold credentials too, and the
    # honest position is that this directory is not shareable. The cost is
    # real -- some users legitimately want a config file -- and that trade is
    # stated here rather than discovered later.
    os.path.join('.config'),
)

# Reasons used in the refusal message, so the UI/log can say WHICH rule fired.
_REASON_PROFILE_STORE = "a Chrome Isolation profile store"
_REASON_APP_DATA = ("Chrome Isolation's data directory "
                   "(profiles.json, proxies.json, host-shares)")
_REASON_HOST_SHARES = "another profile's host-share directory"
_REASON_BROWSER_STORE = "a browser profile store"


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


def _app_owned_roots():
    """
    Every path this app must never hand to a container, as `(root, reason)`
    pairs, each `root` fully resolved so the caller's already-realpath'd path
    can be compared against it directly.

    Two families:

      * THIS APP'S OWN STORAGE. The profile store (every OTHER profile's
        cookies, logins and history — precisely what F1 was opened for), the
        legacy store, and the data directory holding `profiles.json` (all
        profile names plus proxy URLs, which may carry `user:pass@`
        credentials), `proxies.json` and the other profiles' `host-shares/`.
        All of these are read from `backend/paths.py`, the single source of
        truth for where this app keeps data, so relocating the store cannot
        leave a stale copy of the path in the deny-list.

      * REAL BROWSER STORES, from `_BROWSER_PROFILE_DIRS`.

    Computed per call, like `_home_dir()`, because a test (or `su`) can change
    $HOME and `$CHROME_ISOLATION_HOME` between calls.
    """
    home = _home_dir()
    roots = []

    def add(raw, reason):
        if not raw or not isinstance(raw, str):
            return
        try:
            resolved = os.path.realpath(raw)
        except OSError:
            return
        # Two entries must never appear, because either would refuse more than
        # the rule intends: the filesystem root (already refused by the
        # _FORBIDDEN_ROOTS check) and the home directory itself. The home
        # directory is where every OTHER entry below lives, so listing it here
        # would refuse every subfolder of home and silently destroy the
        # "a subfolder of your own home is shareable" behaviour. It is refused
        # exactly once, by _reject_home_itself(), which is checked first.
        if resolved in ('', os.sep) or resolved == home:
            return
        roots.append((resolved, reason))

    # -- this app's own storage, from paths.py -----------------------------
    add(paths.isolation_home(), _REASON_PROFILE_STORE)
    add(paths.default_profiles_dir(), _REASON_PROFILE_STORE)
    add(paths.legacy_profiles_dir(), _REASON_PROFILE_STORE)
    add(paths.resolve_profiles_dir(), _REASON_PROFILE_STORE)
    try:
        add(paths.other_profiles_dir(), _REASON_PROFILE_STORE)
    except OSError:
        pass
    add(APP_DATA_DIR, _REASON_APP_DATA)
    add(HOST_SHARES_DIR, _REASON_HOST_SHARES)

    # -- real browsers' own storage ----------------------------------------
    for sub in _BROWSER_PROFILE_DIRS:
        add(os.path.join(home, sub), _REASON_BROWSER_STORE)

    return roots


def _reject_app_storage(path: str) -> None:
    """
    Raise ValueError if `path` is (or lives inside) this app's own storage or a
    real browser profile store.

    `path` must already be realpath'd — `validate_host_mount()` resolves before
    calling, so a `..` escape and a symlink pointing at any of these roots is
    caught by the same comparison a plainly-typed path is. That is the same
    pair-of-checks shape as `_reject_home_itself`/`_reject_sensitive`, and it
    is why `~/Isolated-Chrome/profiles/Zoom-1/../Fiverr-Read/Default` and
    `~/share -> ~/Isolated-Chrome/profiles` are both refused.
    """
    for root, reason in _app_owned_roots():
        if _is_within(path, root):
            raise ValueError(
                f"Refusing to mount {reason}: {path}. "
                "The host folder must be a dedicated directory you keep for "
                "sharing, not profile data."
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
      * this app's own storage — the profile store (this or any other), the
        legacy store, and the data directory holding `profiles.json`,
        `proxies.json` and the other profiles' `host-shares/`
      * real browsers' own storage (~/.config/google-chrome, ~/.mozilla, …)
      * path traversal or symlinks that resolve into any of the above
      * anything that is not an existing directory

    Note the order: the deny-lists are consulted BEFORE `os.path.isdir()`, so a
    denied path is refused as a policy violation whether or not it exists.
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
    # This app's own storage and real browsers' own storage. `resolved` is
    # already realpath'd, so `..` and symlink spellings of these roots are
    # caught by the same comparison a plainly-typed path is.
    _reject_app_storage(resolved)

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
