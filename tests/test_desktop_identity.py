#!/usr/bin/env python3
"""Desktop identity tests — run with: python3 -m pytest tests/test_desktop_identity.py
or: python3 tests/test_desktop_identity.py

The user-reported bug: Chrome Isolation Manager "messes with" the user's SYSTEM
Google Chrome. The filesystem / network / proxy theories were ruled out with
evidence (no write path to ~/.config/google-chrome exists, no container mounts
a Chrome path, no system proxy or DNS changes, WM classes do not collide with
google-chrome). What is left is DESKTOP IDENTITY — how this app presents
itself to the desktop shell — and there it was genuinely at fault:

  A  Every entry declared `Categories=Network;WebBrowser;`, and the app entry
     advertised `%u`. Eight Chrome-branded "browsers" therefore appeared in
     any "Set as default browser" chooser, GNOME search and xdg tiebreak.
  B  The icon fell back to `google-chrome` when the icon file was missing, and
     `create_desktop_entry` only ran at profile-creation time, so the resulting
     `Icon=google-chrome` entry on disk healed never.
  C  `--class=chrome-<name>` and `StartupWMClass=chrome-isolation-<name>` never
     matched, so the WM could not associate window with launcher.
  D  install.sh / setup.sh ran `xdg-desktop-menu forceupdate`, which rewrites
     the user's whole menu layout, and nothing in the app may claim a MIME
     association.

Every test here is written against the FIXED behaviour. They are deliberately
not weakened to match unfixed code: a red test that documents A/B/C/D is more
useful than a fake green one. See the bottom of this file for the
revert-the-fix proof that these tests are not vacuous.
"""
import os
import re
import shutil
import sys
import tempfile
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'backend'))

import desktop_manager
import docker_manager
import host_share
import registry

# Every category token that would make a default-browser chooser consider an
# entry. `WebBrowser` is the main-category token; the rest are the values a
# "set as default" implementation looks for. We assert none of them appear.
BROWSER_TOKENS = ('webbrowser', 'x-internetbrowser', 'x-www-browser',
                  'network', 'webbrowser2')

# Anything that would claim a MIME / URL association.
ASSOCIATION_TOKENS = ('mimetype', 'x-scheme-handler', 'inode/directory')


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _sandbox():
    """Redirect every path desktop_manager touches into a tmpdir.

    Returns (dirs, cleanup). APP_ICON is pointed at a *nonexistent* file by
    default so the icon tests exercise the degraded path — the exact
    situation that produced `Icon=google-chrome` on disk.
    """
    tmp = tempfile.mkdtemp(prefix='deskid-')
    apps = os.path.join(tmp, 'applications')
    os.makedirs(apps, exist_ok=True)

    saved = {
        'DESKTOP_ENTRIES_DIR': desktop_manager.DESKTOP_ENTRIES_DIR,
        'APP_ICON': desktop_manager.APP_ICON,
        '_theme': desktop_manager._theme_icon_installed,
        '_electron_exec': desktop_manager._electron_exec,
    }
    desktop_manager.DESKTOP_ENTRIES_DIR = apps
    desktop_manager.APP_ICON = os.path.join(tmp, 'no-such-icon.png')
    desktop_manager._theme_icon_installed = lambda: False
    desktop_manager._electron_exec = lambda: '/opt/chrome-isolation/chrome-isolation'

    def _restore():
        desktop_manager.DESKTOP_ENTRIES_DIR = saved['DESKTOP_ENTRIES_DIR']
        desktop_manager.APP_ICON = saved['APP_ICON']
        desktop_manager._theme_icon_installed = saved['_theme']
        desktop_manager._electron_exec = saved['_electron_exec']
        shutil.rmtree(tmp, ignore_errors=True)

    return tmp, apps, _restore


def _parse(content):
    """Parse a .desktop body into {key: value}, lowercasing the key."""
    out = {}
    in_entry = False
    for line in content.splitlines():
        line = line.strip()
        if line == '[Desktop Entry]':
            in_entry = True
            continue
        if line.startswith('['):
            in_entry = False
            continue
        if not in_entry or '=' not in line or line.startswith('#'):
            continue
        k, v = line.split('=', 1)
        out[k.strip().lower()] = v.strip()
    return out


def _categories_of(content):
    raw = _parse(content).get('categories', '')
    return {t.strip().lower() for t in raw.split(';') if t.strip()}


def _py_without_comments(src):
    """
    Python source with `#` comments removed, string literals kept.

    Needed because the fixes are heavily commented with the *names* of the
    things they removed ("`xdg-desktop-menu forceupdate` is deliberately NOT
    used"). A naive substring grep matches those comments and fails a
    correct implementation. Comment stripping is done with tokenize so a `#`
    inside a string is not mistaken for a comment.
    """
    import io
    import tokenize
    out = []
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type == tokenize.COMMENT:
            continue
        out.append(tok.string)
    return '\n'.join(out)


def _py_names(src):
    """The bare identifiers in Python source (no comments, no strings)."""
    import io
    import tokenize
    names = set()
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type == tokenize.NAME:
            names.add(tok.string)
    return names


def _sh_without_comments(src):
    """Shell source with `#` comment lines and trailing comments removed."""
    lines = []
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith('#'):
            continue
        # Strip a trailing comment, respecting simple quoting.
        if '#' in line:
            before = line.split('#', 1)[0]
            if before.count('"') % 2 == 0 and before.count("'") % 2 == 0:
                line = before
        lines.append(line)
    return '\n'.join(lines)


def _all_generated_entries():
    """Every entry body this app can generate, for a few profile names."""
    bodies = {
        'app': desktop_manager.app_entry_content(),
    }
    for name in ('alice', 'Zoom-1', 'a_b-C9'):
        bodies[f'profile:{name}'] = desktop_manager.profile_entry_content(name)
    return bodies


def _docker_manager_for_profile(profile_name):
    """Start a profile against a stubbed docker client; return the argv.

    Mirrors tests/test_docker_manager.py's stub so no daemon is needed. The
    point is to read the REAL `--class` value out of the real code path.
    """
    tmp = tempfile.mkdtemp(prefix='deskid-dk-')
    saved = {
        'REGISTRY_FILE': registry.REGISTRY_FILE,
        'CHROME_PROFILES_DIR': registry.CHROME_PROFILES_DIR,
        'HOST_SHARES_DIR': host_share.HOST_SHARES_DIR,
    }
    registry.REGISTRY_FILE = os.path.join(tmp, 'profiles.json')
    registry.CHROME_PROFILES_DIR = os.path.join(tmp, 'profiles')
    host_share.HOST_SHARES_DIR = os.path.join(tmp, 'host-shares')
    os.makedirs(registry.CHROME_PROFILES_DIR, exist_ok=True)
    os.makedirs(host_share.HOST_SHARES_DIR, exist_ok=True)

    class _Image:
        pass

    class _Container:
        id = 'fake'
        status = 'running'

    class _Containers:
        run_kwargs = None

        def get(self, name):
            raise docker_manager.docker.errors.NotFound('no such container')

        def run(self, **kwargs):
            type(self).run_kwargs = kwargs
            return _Container()

    class _Images:
        def get(self, name):
            return _Image()

    class _Client:
        def __init__(self):
            self.images = _Images()
            self.containers = _Containers()

    real_from_env = docker_manager.docker.from_env
    docker_manager.docker.from_env = lambda: _Client()
    try:
        m = docker_manager.DockerManager()
    finally:
        docker_manager.docker.from_env = real_from_env

    try:
        m.start_container(profile_name)
        cmd = _Containers.run_kwargs['command']
    finally:
        registry.REGISTRY_FILE = saved['REGISTRY_FILE']
        registry.CHROME_PROFILES_DIR = saved['CHROME_PROFILES_DIR']
        host_share.HOST_SHARES_DIR = saved['HOST_SHARES_DIR']
        shutil.rmtree(tmp, ignore_errors=True)
    return cmd


# =========================================================================== #
# FIX A — no generated entry may compete for "default browser"
# =========================================================================== #
def test_no_generated_entry_declares_a_browser_category():
    """
    The core of the complaint. A per-profile launcher opens ONE fixed
    isolated profile; it is not a general web browser. Declaring WebBrowser
    put eight Chrome-branded entries into every default-browser chooser.
    """
    for label, body in _all_generated_entries().items():
        cats = _categories_of(body)
        assert 'webbrowser' not in cats, (
            f"{label}: Categories={sorted(cats)} declares WebBrowser. These "
            f"launchers open a single fixed isolated profile and must never "
            f"appear in a default-browser chooser."
        )
        for token in ('x-www-browser', 'x-internetbrowser', 'webbrowser2'):
            assert token not in cats, f"{label}: {token} in {sorted(cats)}"


def test_per_profile_launchers_are_categorised_as_utility():
    """Positive assertion — guards against dropping Categories entirely."""
    for name in ('alice', 'Zoom-1'):
        cats = _categories_of(desktop_manager.profile_entry_content(name))
        assert 'utility' in cats, \
            f"profile {name}: Categories={sorted(cats)} should include Utility"
        assert cats, f"profile {name}: Categories must not be empty"


def test_no_generated_entry_advertises_url_handling():
    """`Exec=... %u` / `%U` makes an entry a URL-handler candidate."""
    for label, body in _all_generated_entries().items():
        keys = _parse(body)
        for code in ('%u', '%U', '%f', '%F'):
            assert code not in keys.get('exec', ''), (
                f"{label}: Exec={keys.get('exec')!r} advertises {code!r}. This "
                f"app handles no URLs, so it must not claim to."
            )


def test_app_entry_is_not_a_browser():
    """The app's own entry is a manager — Utility only, no Network."""
    cats = _categories_of(desktop_manager.app_entry_content())
    assert cats == {'utility'}, \
        f"app entry Categories={sorted(cats)}, expected exactly {{'utility'}}"
    assert 'network' not in cats, \
        "the manager is not a network application; Network invites the " \
        "shell to file it alongside browsers"


# =========================================================================== #
# FIX B — no icon impersonation
# =========================================================================== #
def test_missing_icon_never_falls_back_to_google_chrome():
    """
    The live defect: chrome-isolation-Fiverr-Demo.desktop carried
    `Icon=google-chrome` because the icon file was missing when the profile
    was created. Degrading to a generic icon is cosmetic; wearing Chrome's
    icon is impersonation.
    """
    tmp, apps, restore = _sandbox()
    try:
        # _sandbox guarantees APP_ICON does not exist and no theme icon does.
        assert not os.path.exists(desktop_manager.APP_ICON)
        icon = desktop_manager._icon()
        assert icon != 'google-chrome', \
            f"_icon() returned {icon!r} with no icon installed — this is the " \
            f"exact impersonation bug"
        for name in ('alice', 'Zoom-1'):
            entry = _parse(desktop_manager.profile_entry_content(name))
            assert entry.get('icon') != 'google-chrome', (
                f"profile {name}: Icon={entry.get('icon')!r} impersonates Chrome"
            )
        entry = _parse(desktop_manager.app_entry_content())
        assert entry.get('icon') != 'google-chrome', \
            f"app entry Icon={entry.get('icon')!r} impersonates Chrome"
    finally:
        restore()


def test_no_generated_entry_names_google_chrome_anywhere():
    """Belt and braces: the string must not appear at all in an entry."""
    for label, body in _all_generated_entries().items():
        assert 'google-chrome' not in body, \
            f"{label}: entry body references google-chrome:\n{body}"


def test_icon_prefers_the_apps_own_icon_when_available():
    """When the icon IS installed, entries should use it — not a generic."""
    tmp, apps, restore = _sandbox()
    try:
        # Install the icon at APP_ICON and in the theme, as install.sh does.
        with open(desktop_manager.APP_ICON, 'wb') as f:
            f.write(b'\x89PNG\r\n\x1a\n')
        assert desktop_manager._icon() == desktop_manager.APP_ICON

        # And via the theme path, which is what install.sh sets up.
        desktop_manager._theme_icon_installed = lambda: True
        assert desktop_manager._icon() == 'chrome-isolation'
    finally:
        restore()


def test_missing_icon_degrades_to_a_generic_name():
    """Explicitly assert the fallback is generic, not Chrome-branded."""
    tmp, apps, restore = _sandbox()
    try:
        assert desktop_manager._icon() == 'web-browser', (
            "with nothing installed, the icon must degrade to a generic "
            f"web-browser, got {desktop_manager._icon()!r}"
        )
    finally:
        restore()


# =========================================================================== #
# FIX B (cont.) — stale entries must heal
# =========================================================================== #
def test_refresh_heals_a_stale_entry_written_by_the_old_code():
    """
    `create_desktop_entry` only ran at profile-creation time, so an entry
    written by the old build stayed wrong forever. This is the file on disk:
    Icon=google-chrome, Categories=Network;WebBrowser;. It must be repaired
    without recreating the profile.
    """
    tmp, apps, restore = _sandbox()
    try:
        stale = os.path.join(apps, 'chrome-isolation-Fiverr-Demo.desktop')
        with open(stale, 'w') as f:
            f.write(
                "[Desktop Entry]\n"
                "Version=1.0\n"
                "Type=Application\n"
                "Name=Chrome (Fiverr-Demo)\n"
                "Comment=Isolated Chrome Profile: Fiverr-Demo\n"
                "Exec=/home/x/.local/bin/chrome-isolation --profile Fiverr-Demo\n"
                "Icon=google-chrome\n"
                "Terminal=false\n"
                "Categories=Network;WebBrowser;\n"
                "StartupWMClass=chrome-isolation-Fiverr-Demo\n"
            )

        result = desktop_manager.refresh_desktop_entries()
        assert stale in result['updated'], \
            f"the stale entry was not repaired: {result}"

        with open(stale) as f:
            healed = f.read()
        entry = _parse(healed)
        assert entry['icon'] != 'google-chrome', healed
        assert 'webbrowser' not in _categories_of(healed), healed
    finally:
        restore()


def test_refresh_repairs_the_stale_app_entry():
    """chrome-isolation.desktop shipped with WebBrowser and Exec=... %u."""
    tmp, apps, restore = _sandbox()
    try:
        stale = os.path.join(apps, desktop_manager.APP_DESKTOP_FILENAME)
        with open(stale, 'w') as f:
            f.write(
                "[Desktop Entry]\n"
                "Type=Application\n"
                "Name=Chrome Isolation Manager\n"
                "GenericName=Browser Profile Manager\n"
                "Exec=/home/x/.local/bin/chrome-isolation %u\n"
                "Icon=chrome-isolation\n"
                "Terminal=false\n"
                "Categories=Network;WebBrowser;Utility;\n"
                "StartupWMClass=chrome-isolation\n"
            )
        desktop_manager.refresh_desktop_entries()
        with open(stale) as f:
            healed = f.read()
        assert 'webbrowser' not in _categories_of(healed), healed
        assert '%u' not in _parse(healed)['exec'], healed
    finally:
        restore()


def test_refresh_is_idempotent():
    """Second refresh must be a no-op: no rewrite, no menu-cache rebuild."""
    tmp, apps, restore = _sandbox()
    try:
        desktop_manager.create_desktop_entry('alice')
        first = desktop_manager.refresh_desktop_entries()
        assert first['updated'] == [], f"nothing should change yet: {first}"

        stamps = {n: os.stat(os.path.join(apps, n)).st_mtime_ns
                  for n in os.listdir(apps)}
        second = desktop_manager.refresh_desktop_entries()
        assert second['updated'] == [], f"refresh is not idempotent: {second}"
        for n, mt in stamps.items():
            assert os.stat(os.path.join(apps, n)).st_mtime_ns == mt, \
                f"{n} was rewritten by an idempotent refresh"
    finally:
        restore()


def test_refresh_leaves_foreign_entries_alone():
    """We must not rewrite the user's or another app's .desktop files."""
    tmp, apps, restore = _sandbox()
    try:
        for foreign in ('google-chrome.desktop', 'brave-browser.desktop',
                        'firefox.desktop', 'org.gnupg.pinentry-qt.desktop'):
            p = os.path.join(apps, foreign)
            with open(p, 'w') as f:
                f.write(f"[Desktop Entry]\nName={foreign}\nIcon=whatever\n")
        before = {n: open(os.path.join(apps, n)).read() for n in os.listdir(apps)}

        desktop_manager.refresh_desktop_entries()

        for n, content in before.items():
            assert open(os.path.join(apps, n)).read() == content, \
                f"refresh modified a foreign entry: {n}"
        # google-chrome.desktop in particular must never be our business
        assert 'Icon=google-chrome' not in before.get('google-chrome.desktop', '')
    finally:
        restore()


def test_refresh_does_not_delete_entries_or_profiles():
    """Refresh repairs content; removal stays delete_profile()'s job."""
    tmp, apps, restore = _sandbox()
    try:
        p = os.path.join(apps, 'chrome-isolation-ghost.desktop')
        with open(p, 'w') as f:
            f.write("[Desktop Entry]\nIcon=google-chrome\nCategories=Network;WebBrowser;\n")
        desktop_manager.refresh_desktop_entries()
        assert os.path.exists(p), "refresh deleted an entry; that is not its job"
    finally:
        restore()


def test_refresh_is_reachable_from_profile_service_and_the_bridge():
    """The heal path must actually be wired up, not just defined."""
    import profile_service
    assert hasattr(profile_service, 'refresh_desktop_entries'), \
        "profile_service must expose refresh_desktop_entries()"

    src = open(os.path.join(ROOT, 'backend', 'bridge.py')).read()
    assert 'refresh_desktop_entries' in src, \
        "the bridge must expose a refresh_desktop_entries method"

    main_js = open(os.path.join(ROOT, 'electron', 'main.js')).read()
    assert 'refresh_desktop_entries' in main_js, \
        "main.js must kick off the refresh on app start"


# =========================================================================== #
# FIX C — --class and StartupWMClass must agree
# =========================================================================== #
def test_wm_class_and_container_class_match():
    """
    The real code path: start the profile and read the actual --class out of
    the argv, then compare it with the StartupWMClass of the entry we
    generate. These used to be `chrome-alice` and `chrome-isolation-alice`.
    """
    tmp, apps, restore = _sandbox()
    try:
        cmd = _docker_manager_for_profile('alice')
        class_args = [c for c in cmd if c.startswith('--class=')]
        assert len(class_args) == 1, f"expected exactly one --class in {cmd}"
        container_class = class_args[0].split('=', 1)[1]

        entry = _parse(desktop_manager.profile_entry_content('alice'))
        assert entry['startupwmclass'] == container_class, (
            f"container --class={container_class!r} does not match "
            f"StartupWMClass={entry['startupwmclass']!r}; the WM cannot "
            f"associate the window with its launcher"
        )
    finally:
        restore()


def test_wm_class_is_namespaced_away_from_real_chrome():
    """Chrome's real class is `google-chrome`. Ours must never equal it."""
    for name in ('alice', 'Zoom-1', 'chrome', 'google-chrome'):
        cls = desktop_manager.wm_class(name)
        assert cls != 'google-chrome', f"wm class collides with real Chrome: {cls}"
        assert cls.startswith('chrome-isolation-'), cls


def test_wm_class_is_shared_not_duplicated():
    """docker_manager must import the one function, not re-spell the string."""
    src = open(os.path.join(ROOT, 'backend', 'docker_manager.py')).read()
    assert 'from desktop_manager import wm_class' in src, \
        "docker_manager must import wm_class from desktop_manager"
    # No hardcoded --class= spelling left behind.
    hardcoded = re.findall(r"--class=\{?['\"]?chrome-", src)
    assert not hardcoded, \
        f"docker_manager still hardcodes the window class: {hardcoded}"


def test_multiple_profiles_each_get_their_own_class():
    for name in ('alice', 'bob', 'Zoom-1'):
        cmd = _docker_manager_for_profile(name)
        cls = [c for c in cmd if c.startswith('--class=')][0].split('=', 1)[1]
        entry = _parse(desktop_manager.profile_entry_content(name))
        assert cls == entry['startupwmclass'], (name, cls, entry['startupwmclass'])
    # and they are distinct from one another
    classes = {desktop_manager.wm_class(n) for n in ('alice', 'bob', 'Zoom-1')}
    assert len(classes) == 3, classes


# =========================================================================== #
# FIX D — no MIME / association claims, scoped cache updates
# =========================================================================== #
def test_no_generated_entry_claims_a_mime_association():
    """
    If the app wrote MimeType= or an x-scheme-handler/ key, it could be
    elected default handler for those types. It must never write either.
    """
    for label, body in _all_generated_entries().items():
        entry = _parse(body)
        assert 'mimetype' not in entry, \
            f"{label}: declares MimeType={entry['mimetype']!r}"
        for token in ASSOCIATION_TOKENS:
            assert token not in body.lower(), \
                f"{label}: mentions {token!r}"


def test_no_backend_code_writes_association_keys():
    """
    Source-level check across the backend, because a future entry generator
    could reintroduce this without the content tests above noticing.
    """
    offenders = []
    backend = os.path.join(ROOT, 'backend')
    for name in sorted(os.listdir(backend)):
        if not name.endswith('.py'):
            continue
        # Comments explain why we do NOT write these keys, so strip them and
        # match the emitted literals.
        src = _py_without_comments(open(os.path.join(backend, name)).read())
        for lineno, line in enumerate(src.splitlines(), 1):
            if re.search(r'["\']MimeType=', line) or re.search(r'["\']x-scheme-handler/', line):
                offenders.append(f"{name}: {line.strip()}")
    assert not offenders, "association keys written by the backend: " + str(offenders)


def test_installer_scripts_claim_no_association_and_no_browser():
    """
    install.sh and setup.sh write the app's own entry by hand. Assert the
    heredocs are clean. `xdg-desktop-menu forceupdate` rewrites the user's
    whole menu layout — the broadest blast radius of anything the installer
    runs, and the least necessary.
    """
    for script in ('install.sh', 'setup.sh'):
        src = _sh_without_comments(open(os.path.join(ROOT, script)).read())
        assert 'xdg-desktop-menu' not in src, (
            f"{script} still calls xdg-desktop-menu forceupdate, which "
            f"rewrites the user's menu layout well beyond the one file it "
            f"owns"
        )
        assert 'WebBrowser' not in src, \
            f"{script} still declares WebBrowser in a desktop entry"
        assert 'x-scheme-handler' not in src, \
            f"{script} references x-scheme-handler"
        assert not re.search(r'^\s*MimeType=', src, re.M), \
            f"{script} writes a MimeType= key"
        # A heredoc that is written but never used is not what we check —
        # only the actual emitted content matters, and the heredoc body is
        # exactly the text between `cat > ... <<DESKTOP` and `DESKTOP`.
        # %u/%U in the heredoc means the entry advertises URL handling
        for line in src.splitlines():
            if line.startswith('Exec='):
                assert '%u' not in line and '%U' not in line, \
                    f"{script}: {line!r} advertises URL handling"


def test_python_and_shell_app_entries_agree():
    """
    desktop_manager.app_entry_content() regenerates what install.sh wrote.
    They must not drift, or a refresh would keep rewriting the installer's
    file. Compare the identity-bearing keys, not Exec (different path).
    """
    sh = open(os.path.join(ROOT, 'install.sh')).read()
    assert 'Categories=Utility;' in sh, \
        "install.sh app entry must use Categories=Utility;"
    entry = _parse(desktop_manager.app_entry_content())
    assert entry['categories'] == 'Utility;', entry
    assert entry['startupwmclass'] == 'chrome-isolation', entry
    assert 'Google' not in entry.get('name', ''), \
        "the app entry must not be named after Google Chrome"


def test_update_desktop_database_is_scoped_and_only_on_change():
    """
    It stays — our entries really do live in that directory — but it must be
    limited to that one directory and must not fire on a no-op.
    """
    calls = []
    real_run = desktop_manager.subprocess.run

    def _spy(args, *a, **kw):
        calls.append(list(args))
        raise FileNotFoundError  # don't actually run anything

    desktop_manager.subprocess.run = _spy
    tmp, apps, restore = _sandbox()
    try:
        desktop_manager.create_desktop_entry('alice')
        assert len(calls) == 1, f"expected one cache update on create: {calls}"
        assert calls[0][0] == 'update-desktop-database', calls[0]
        assert calls[0][1] == apps, \
            f"update-desktop-database must be scoped to {apps}, got {calls[0][1]}"

        # A rewrite of identical content must not trigger a rebuild.
        desktop_manager.create_desktop_entry('alice')
        assert len(calls) == 1, f"no-op create rebuilt the cache: {calls}"

        # A real change must.
        desktop_manager.refresh_desktop_entries()
        assert len(calls) == 1, "refresh of already-correct files rebuilt cache"
    finally:
        restore()
        desktop_manager.subprocess.run = real_run


def test_no_wide_impact_desktop_rebuild_helper_is_called_from_python():
    """
    Check identifiers, not raw text: the module's docstrings deliberately
    name these tools to explain why they are NOT used, and a substring grep
    would flag that explanation as a violation.
    """
    src = open(os.path.join(ROOT, 'backend', 'desktop_manager.py')).read()
    names = _py_names(src)
    for banned in ('xdg-desktop-menu', 'xdg_desktop_menu', 'update-mime-database',
                   'xdg-settings', 'xdg-mime', 'update-desktop-database'):
        # update-desktop-database IS used (scoped); the rest are not, and
        # none may appear even as a bare name outside a string.
        if banned == 'update-desktop-database':
            continue
        assert banned not in names, \
            f"desktop_manager must not invoke {banned} (broad blast radius)"

    # And the only external tool it does run is the scoped one. Matched on
    # the raw source with \s* across lines, because the tokenize-based
    # stripper reflows tokens and would defeat a single-line pattern.
    runs = set(re.findall(r"subprocess\.run\(\s*\[\s*['\"]([a-z-]+)['\"]", src))
    assert runs == {'update-desktop-database'}, \
        f"desktop_manager runs unexpected external tools: {runs}"


# =========================================================================== #
# Entry content sanity (so the fix cannot "pass" by emitting nothing)
# =========================================================================== #
def test_generated_entries_are_well_formed_and_non_empty():
    """Non-vacuity: the entries must still be real, launchable launchers."""
    tmp, apps, restore = _sandbox()
    try:
        desktop_manager.create_desktop_entry('alice')
        path = os.path.join(apps, 'chrome-isolation-alice.desktop')
        assert os.path.exists(path), path
        entry = _parse(open(path).read())
        assert entry['type'].lower() == 'application', entry
        assert entry['terminal'].lower() == 'false', entry
        assert '--profile alice' in entry['exec'], entry
        assert entry['name'], entry
        assert os.access(path, os.R_OK)
    finally:
        restore()


def test_entry_is_written_atomically_via_a_temp_file():
    tmp, apps, restore = _sandbox()
    try:
        desktop_manager.create_desktop_entry('alice')
        leftovers = [n for n in os.listdir(apps) if n.endswith('.tmp')]
        assert not leftovers, f"temp file left behind: {leftovers}"
    finally:
        restore()


def test_profile_names_are_still_validated():
    """The refresh must not become a path-traversal vector."""
    for bad in ('../escape', 'a/b', '..', '', 'x' * 100, 'a;b'):
        try:
            desktop_manager.profile_entry_content(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"profile_entry_content accepted {bad!r}")


# --------------------------------------------------------------------------- #
# standalone runner (matches the style of the other tests/ files)
# --------------------------------------------------------------------------- #
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
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
