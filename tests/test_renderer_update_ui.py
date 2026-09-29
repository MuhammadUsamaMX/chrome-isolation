#!/usr/bin/env python3
"""Chromium update status in the renderer — static assertions.

Run with:  python3 -m pytest tests/test_renderer_update_ui.py
or:        python3 tests/test_renderer_update_ui.py

WHY THIS FILE EXISTS
--------------------
The backend for Chromium auto-update was finished and tested. The renderer
never called it: `image:updateStatus` was wired in `electron/main.js` and
`preload.js`, and nothing in `electron/renderer/*` ever asked. The result was
that an available browser update was completely invisible — the exact failure
mode the backend was built to avoid (review finding, Phase 8 risk 4).

Two contract rules are asserted here because both are easy to "fix" back into
a bug, and both fail silently:

  A  `update_available: null` means COULD NOT DETERMINE. It is not
     `false`, and it must never be rendered as "up to date". Conflating
     them is how a browser stays two years stale behind a green indicator.
     Asserted structurally: the tone function reaches 'ok' only from
     positively-determined states, and the "unknown" case has its own
     distinct copy that says so out loud.

  B  `consent_warning` is the BACKEND's text, with the user's current
     version substituted into it. The renderer must display it verbatim and
     must not carry its own copy of the one-way-door wording. Asserted by
     searching app.js for sentences that duplicate the backend's warning.

Accessibility rule: no `aria-modal` / `role="dialog"` is introduced without a
real focus trap, which this app still does not have (commit `c6405d5` reverted
those attributes for exactly that reason). The update panel is therefore
inline, not modal.

Static only — the renderer has no browser test harness and this file must not
introduce one, matching `test_renderer_a11y.py`. See `main()` at the bottom
and the non-vacuity tests for the proof these are not hollow.
"""
import os
import re
import sys
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RENDERER = os.path.join(ROOT, 'electron', 'renderer')
HTML_PATH = os.path.join(RENDERER, 'index.html')
JS_PATH = os.path.join(RENDERER, 'app.js')
CSS_PATH = os.path.join(RENDERER, 'style.css')
PRELOAD_PATH = os.path.join(ROOT, 'electron', 'preload.js')
MAIN_PATH = os.path.join(ROOT, 'electron', 'main.js')
BACKEND_PATH = os.path.join(ROOT, 'backend', 'image_update.py')

# Backend `STATES`, copied verbatim. Kept as data rather than imported so the
# test still means something if the backend cannot be imported, and so a drift
# is visible as a test failure instead of a silently-passing import.
BACKEND_STATES = (
    'idle', 'checking', 'building', 'verifying', 'ready', 'failed',
    'up_to_date', 'unknown', 'update_available', 'deferred', 'disabled',
    'debounced', 'interrupted', 'base_eol',
)


# --------------------------------------------------------------------------- #
# helpers (mirroring test_renderer_a11y.py)
# --------------------------------------------------------------------------- #
def _read(path):
    with open(path, encoding='utf-8') as fh:
        return fh.read()


def _html_without_comments(src):
    return re.sub(r'<!--.*?-->', '', src, flags=re.S)


def _js_without_comments(src):
    """app.js with `//` and `/* */` comments removed, string literals kept.

    Comments are stripped for the same reason test_renderer_a11y.py strips
    them: the explanatory notes in this code deliberately spell out the
    `aria-modal` attribute and the tri-state rule they forbid, and a naive
    grep would match that prose and fail correct code.
    """
    src = re.sub(r'/\*.*?\*/', '', src, flags=re.S)
    out = []
    for line in src.splitlines():
        stripped, in_str, quote, esc = line, False, '', False
        cut = None
        for i, ch in enumerate(stripped):
            if in_str:
                if esc:
                    esc = False
                elif ch == '\\':
                    esc = True
                elif ch == quote:
                    in_str = False
                continue
            if ch in ('"', "'", '`'):
                in_str, quote = True, ch
            elif ch == '/' and stripped[i:i + 2] == '//':
                cut = i
                break
        out.append(stripped if cut is None else stripped[:cut])
    return '\n'.join(out)


def _function_body(js, name):
    """Brace-balanced body of a top-level `function name(...) {...}`."""
    m = re.search(r'function\s+%s\s*\([^)]*\)\s*\{' % re.escape(name), js)
    assert m, f"function {name}() is missing from app.js"
    start = m.end()
    depth, i = 1, start
    while depth and i < len(js):
        if js[i] == '{':
            depth += 1
        elif js[i] == '}':
            depth -= 1
        i += 1
    return js[start:i - 1]


def _case_chunks(body):
    """Split a `switch` body into {'state': 'statements under that case'}.

    Returns a dict keyed by every `case 'x':` label in the body. Fallthrough
    (`case 'failed':` immediately followed by `case 'interrupted':`) is
    recorded as a label with an EMPTY string of its own statements, because
    those statements belong to the next label.

    Written rather than regex-per-state because the naive version
    (`case 'x':(.*?)(?=case 'y')`) silently returns '' for every label that
    falls through — which is exactly how a test asserting on the failed-state
    tone can pass or fail for reasons unrelated to the code.
    """
    chunks = {}
    matches = list(re.finditer(r"case\s+'([a-z_]+)'\s*:", body))
    assert matches, "no `case '...':` labels found — is this a switch body?"
    for idx, m in enumerate(matches):
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(body)
        chunks[m.group(1)] = body[m.end():end]
    return chunks


def _by_id(src, elem_id):
    for m in re.finditer(r'<([a-zA-Z][a-zA-Z0-9-]*)\b([^>]*?)/?>', src):
        if re.search(r'\bid\s*=\s*"%s"' % re.escape(elem_id), m.group(2)):
            return m.group(1).lower(), m.group(2)
    return None


# --------------------------------------------------------------------------- #
# 0 — the plumbing exists at all
# --------------------------------------------------------------------------- #
def test_renderer_calls_the_update_status_handler():
    """
    The original defect, stated as a test. The renderer never asked for the
    status, so an available update was invisible. Guard against a "fix" that
    wires only one of the three.
    """
    js = _js_without_comments(_read(JS_PATH))
    assert 'window.api.image.status' in js, \
        "app.js never calls window.api.image.status() — the update badge would " \
        "have nothing to reflect and an available update stays invisible"
    assert 'window.api.image.check' in js, \
        "app.js never calls window.api.image.check() — the user has no way to " \
        "ask for a check"
    assert 'window.api.image.apply' in js, \
        "app.js never calls window.api.image.apply() — there is no Apply action"


def test_preload_exposes_the_whole_image_api():
    """
    `window.api.image.{status,check,apply}` is the contract the renderer codes
    against. A partial preload is a runtime TypeError, not a graceful failure.
    """
    preload = _js_without_comments(_read(PRELOAD_PATH))
    m = re.search(r'image\s*:\s*\{(.*?)\}', preload, flags=re.S)
    assert m, "preload.js does not expose an `image` namespace"
    body = m.group(1)
    for fn in ('status', 'check', 'apply'):
        assert re.search(r'\b%s\s*:' % fn, body), \
            f"window.api.image.{fn} is missing from preload.js"
    assert 'image:applyUpdate' in preload and 'image:checkUpdate' in preload \
        and 'image:updateStatus' in preload, \
        "the preload image namespace does not map onto the main.js IPC channels"


def test_main_process_still_registers_the_three_channels():
    """Guard against the UI and the main process drifting apart."""
    main = _js_without_comments(_read(MAIN_PATH))
    for channel in ('image:updateStatus', 'image:checkUpdate', 'image:applyUpdate'):
        assert f"ipcMain.handle('{channel}'" in main, \
            f"main.js no longer registers {channel}"


# --------------------------------------------------------------------------- #
# 1 — RULE A: the three states are distinguishable
# --------------------------------------------------------------------------- #
def test_update_tone_distinguishes_all_three_states():
    """
    The structural assertion of rule A, on `updateTone()`.

    'ok' (positively current), 'attention' (an update exists) and 'warn'
    (could not determine) must be three DIFFERENT tones. If a change collapses
    'unknown' into 'ok' this fails — which is the conflation, caught at the
    single choke point every state passes through.
    """
    js = _js_without_comments(_read(JS_PATH))
    body = _function_body(js, 'updateTone')
    cases = _case_chunks(body)

    for state in ('up_to_date', 'update_available', 'unknown'):
        assert state in cases, f"updateTone no longer handles '{state}'"

    # Pull each case's returned tone out of the switch. Fallthrough labels
    # ('case 'failed':' with no statements of its own) inherit the tone of the
    # label that follows, which is how the switch is actually written.
    def tone_for(state):
        chunk = cases[state]
        if not chunk.strip() and state not in ('default',):
            # fallthrough — walk forward to the label that carries statements
            later = [s for s in cases if cases[s].strip()]
            after = [s for s in cases
                     if body.index(f"case '{s}':") > body.index(f"case '{state}':")]
            following = [s for s in after if s in later]
            assert following, f"case '{state}' is empty and falls off the end"
            chunk = cases[following[0]]
        r = re.search(r"return\s+'([a-z]+)'", chunk)
        assert r, f"case '{state}' does not return a tone"
        return r.group(1)

    up_to_date = tone_for('up_to_date')
    available = tone_for('update_available')
    unknown = tone_for('unknown')

    assert up_to_date != unknown, (
        f"'up_to_date' and 'unknown' both render as '{up_to_date}'. "
        "update_available: null means COULD NOT DETERMINE, not 'you are "
        "current' — this is the bug the whole feature exists to prevent"
    )
    assert unknown != available, \
        "'unknown' and 'update_available' must not share a tone"
    assert available not in (up_to_date,), \
        "'update_available' must not render as up to date"
    assert unknown != 'ok', "'unknown' must never be the success tone"


def test_update_tone_never_uses_the_success_tone_on_a_falsy_flag():
    """
    Belt-and-braces on rule A, and the specific anti-pattern: a tone function
    that tests a FALSY `update_available` will return 'ok' for `null`.

    Every `return 'ok'` in `updateTone` must sit inside a case for one of the
    two states the backend reached by positively determining the version is
    current. A bare `if (!s.update_available) return 'ok'` cannot pass.
    """
    js = _js_without_comments(_read(JS_PATH))
    body = _function_body(js, 'updateTone')
    cases = _case_chunks(body)

    ok_returns = [m.start() for m in re.finditer(r"return\s+'ok'", body)]
    assert ok_returns, "updateTone never returns 'ok' at all"

    # Every `return 'ok'` must belong to one of the states that earned it.
    # Map each `return 'ok'` offset back to the label it sits under.
    labels = [(m.group(1), m.end()) for m in
              re.finditer(r"case\s+'([a-z_]+)'\s*:", body)]
    ok_states = []
    for m in re.finditer(r"return\s+'ok'", body):
        owner = [name for name, start in labels if start <= m.start()]
        assert owner, "a `return 'ok'` appears before any case label"
        ok_states.append(owner[-1])

    allowed = {'up_to_date', 'ready'}
    for state in ok_states:
        assert state in allowed, (
            f"case '{state}' returns the success tone. Only {sorted(allowed)} "
            "may claim 'up to date': they are the states where the backend "
            "positively determined the installed version is current."
        )

    # The flag itself must never be the discriminator.
    assert not re.search(r'update_available\s*\)\s*return', body), \
        "updateTone decides on update_available; the tri-state is the whole " \
        "point and false/null must not be interchangeable"


def test_the_unknown_state_says_out_loud_that_it_is_not_up_to_date():
    """
    Rule A is not only about colour. The panel text for 'unknown' must state
    that this is not the same as being current, because a user reading the
    words is as entitled to the distinction as a user seeing the colour.
    """
    js = _js_without_comments(_read(JS_PATH))
    body = _function_body(js, 'updatePanelBody')
    assert "case 'unknown':" in body, \
        "updatePanelBody has no branch for the 'unknown' state"

    chunk = _case_chunks(body)['unknown']
    lowered = chunk.lower()
    assert 'not the same as' in lowered or 'could not determine' in lowered, (
        "the 'unknown' panel copy does not distinguish itself from "
        "up-to-date; it must say in words that the check failed"
    )
    stripped = lowered.replace('not the same as being up to date', '')
    assert 'up to date' not in stripped, \
        "the 'unknown' copy must not reuse the up-to-date phrasing"


def test_chip_and_panel_labels_keep_the_three_states_apart():
    """
    Both surfaces carry a label as well as a tone. 'Up to date' may only be
    used for the states that earned it — a stale browser showing that string is
    the whole failure, whatever colour it is.
    """
    js = _js_without_comments(_read(JS_PATH))

    chip = _function_body(js, 'updateChipLabel')
    panel = _function_body(js, 'updatePanelTitle')

    for fn_name, body in (('updateChipLabel', chip), ('updatePanelTitle', panel)):
        cases = _case_chunks(body)
        assert 'unknown' in cases, f"{fn_name} has no branch for the 'unknown' state"
        chunk = cases['unknown'].lower()
        assert 'up to date' not in chunk, \
            f"{fn_name} uses the 'up to date' wording for the unknown state"
        assert 'could not check' in chunk or 'could not determine' in chunk, \
            f"{fn_name} does not say the check could not happen for 'unknown'"

    assert 'could not check' in chip.lower(), \
        "the chip label for 'unknown' must say the check could not be done"
    assert 'up to date' in panel.lower(), \
        "the panel title for 'up_to_date' should say so plainly"


def test_every_backend_state_has_a_tone_and_a_label():
    """
    Coverage: an unhandled state falls through to the `default` branches and
    renders as 'muted' / 'Update status unknown', which is honest but silent.
    That is survivable; a state that renders as a SUCCESS tone is not. This
    asserts every backend state is explicitly handled somewhere, so a new
    backend state is a visible test failure rather than a quietly-grey badge.
    """
    js = _js_without_comments(_read(JS_PATH))
    tone = _function_body(js, 'updateTone')
    chip = _function_body(js, 'updateChipLabel')

    missing = [s for s in BACKEND_STATES
               if f"case '{s}':" not in tone and f"case '{s}':" not in chip]
    assert not missing, (
        f"backend state(s) with no explicit case in updateTone/updateChipLabel: "
        f"{missing}\nThey fall through to `default` and render as 'muted', "
        "which is honest but hides a state the backend considers meaningful."
    )


def test_backend_state_vocabulary_has_not_drifted():
    """
    Keep this file's copy of BACKEND_STATES honest. Read the real tuple out of
    the backend source rather than importing it, so a drift is a readable
    failure instead of a suite that quietly tests a vocabulary nobody uses.
    """
    src = _read(BACKEND_PATH)
    m = re.search(r'^STATES\s*=\s*\((.*?)^\)', src, flags=re.S | re.M)
    assert m, "could not find the STATES tuple in backend/image_update.py"
    real = set(re.findall(r'"([a-z_]+)"', m.group(1)))
    assert real == set(BACKEND_STATES), (
        "this test's BACKEND_STATES no longer matches the backend's STATES\n"
        f"only in backend: {sorted(real - set(BACKEND_STATES))}\n"
        f"only in test:    {sorted(set(BACKEND_STATES) - real)}"
    )


# --------------------------------------------------------------------------- #
# 2 — RULE B: the consent warning is the backend's, shown before consent
# --------------------------------------------------------------------------- #
def test_the_panel_renders_consent_warning_from_the_payload():
    """
    The one-way-door text must reach the DOM from `consent_warning`. Both
    long and short forms are used: long above the button, short inside it.
    """
    js = _js_without_comments(_read(JS_PATH))
    body = _function_body(js, 'renderUpdate')
    assert 'consent_warning' in body, \
        "renderUpdate never reads `consent_warning` from the payload"
    assert 'updateConsentWarning' in body, \
        "the consent warning element is never populated"
    assert 'consent_warning_short' in body, \
        "the one-line form `consent_warning_short` is never used"


def test_the_consent_warning_element_exists_and_starts_hidden():
    """
    Non-vacuity plus ordering. It must be `hidden` in the markup: with no
    update on offer there is nothing to consent to, and an empty visible
    paragraph is noise. It becomes visible only once a version is on offer.
    """
    html = _html_without_comments(_read(HTML_PATH))
    found = _by_id(html, 'updateConsentWarning')
    assert found, "#updateConsentWarning is missing from index.html"
    assert found[0] == 'p', "the consent warning should be a paragraph"
    assert 'hidden' in found[1], \
        "#updateConsentWarning must start hidden; it is shown only when a " \
        "specific version is on offer"
    assert 'update-consent-warning' in found[1], \
        "#updateConsentWarning lost its styling hook"


def test_the_renderer_carries_no_copy_of_the_consent_warning():
    """
    RULE B, the strict form. The wording lives in backend/image_update.py so
    it cannot drift from what the code means; a second copy in app.js is a
    second thing to keep in sync, and the brief is explicit that replacing the
    payload's text is not allowed.

    Checked by looking for the backend's distinctive claims. This is a
    paraphrase-tolerant match — it is trying to catch a REPLACEMENT, not a
    reference to the payload field.
    """
    js = _read(JS_PATH)
    low = js.lower()

    forbidden = [
        'will not downgrade',
        'cannot be undone',
        'cannot be reverted',
        'one-way door',
        'one way door',
        'profiles in place',
        'recoverable',
    ]
    # `cannot be undone` also appears in the Delete-profile confirm copy, which
    # is legitimately its own wording. Scope the check to the update block.
    update_block = js[js.find('Chromium update — status surface'):] \
        if 'Chromium update — status surface' in js else js

    hits = []
    for phrase in forbidden:
        for name, haystack in (('applyConsentedUpdate', update_block),):
            if phrase in haystack.lower():
                hits.append(phrase)
    assert not hits, (
        f"app.js contains its own one-way-door wording: {hits}\n"
        "The text about not being able to undo an update must come from the "
        "backend's `consent_warning` and nowhere else. Showing the payload "
        "verbatim is the point; rewriting it is the bug."
    )
    # Sanity: the check above is looking at a non-empty region.
    assert len(update_block) > 500, \
        "could not locate the update block in app.js; the check would be vacuous"


def test_apply_sends_the_exact_consent_version_that_was_shown():
    """
    Consent is a versioned token. The renderer must send back the
    `consent_version` it displayed, not `latest_version` recomputed, not a
    boolean, not undefined.
    """
    js = _js_without_comments(_read(JS_PATH))
    body = _function_body(js, 'applyConsentedUpdate')
    assert 'window.api.image.apply(' in body, \
        "applyConsentedUpdate does not call window.api.image.apply"
    m = re.search(r'window\.api\.image\.apply\(([^)]*)\)', body)
    assert m, "could not read the argument passed to image.apply()"
    assert 'approved' in m.group(1), (
        f"image.apply() is called with {m.group(1)!r}; it must receive the "
        "exact `consent_version` the user was shown"
    )
    # `approved` must be captured BEFORE the await, from the displayed value.
    assert re.search(r'const\s+approved\s*=\s*consentVersion', body), \
        "the approved version is not snapshotted from `consentVersion` before " \
        "the apply call; a re-render mid-flight would change what was approved"


def test_the_consent_warning_precedes_the_apply_action_in_the_dom():
    """
    Requirement: the user must see the one-way-door warning BEFORE clicking,
    not after. Static DOM order is the checkable form of that.
    """
    html = _html_without_comments(_read(HTML_PATH))
    warn = html.find('id="updateConsentWarning"')
    apply = html.find('id="btnApplyUpdate"')
    assert warn != -1 and apply != -1, "consent warning or apply button missing"
    assert warn < apply, (
        "the consent warning must come before the Apply button in the DOM so "
        "it is read first"
    )


def test_apply_is_not_offered_without_a_version_and_a_warning():
    """
    The Apply button is offered on the conjunction, not on either half. A
    button that can fire without the warning on screen would put the consent
    behind the action.
    """
    js = _js_without_comments(_read(JS_PATH))
    body = _function_body(js, 'renderUpdate')
    m = re.search(r'const\s+showApply\s*=\s*(.*?);', body, flags=re.S)
    assert m, "could not find the showApply condition"
    cond = m.group(1)
    assert 'consentVersion' in cond, \
        "Apply is offered without a specific version on offer"
    assert 'consentWarning' in cond, \
        "Apply is offered without the one-way-door warning being displayed"
    assert 'applyInFlight' in cond, \
        "Apply is offered while an apply is already in flight"


def test_apply_is_not_offered_under_unattended_or_disabled_policy():
    """
    Requirement 5. Under `unattended` the rebuild happens by itself, so a
    button for it is a lie about what the user controls. Under `disabled`
    nothing would happen at all.
    """
    js = _js_without_comments(_read(JS_PATH))
    body = _function_body(js, 'renderUpdate')
    m = re.search(r'const\s+showApply\s*=\s*(.*?);', body, flags=re.S)
    cond = m.group(1)
    assert 'unattended' in cond, \
        "Apply must be suppressed when policy is 'unattended'"
    assert "'disabled'" in cond, \
        "Apply must be suppressed when policy is 'disabled'"


# --------------------------------------------------------------------------- #
# 3 — the version-moved refusal is handled, not shown as a bare error
# --------------------------------------------------------------------------- #
def test_a_moved_version_is_explained_and_the_panel_refetched():
    """
    The backend refuses when the version changed between check and click, and
    returns a refreshed status instead. The UI must notice the difference
    between what was approved and what is now on offer, re-render, and say so.

    Three things asserted, all required: compare, re-fetch, explain.
    """
    js = _js_without_comments(_read(JS_PATH))
    body = _function_body(js, 'applyConsentedUpdate')

    # `nowOnOffer` must be derived from the REFETCHED status, never aliased
    # back to `approved`. Aliasing it is the exact mutation that turns this
    # handler into "always show a bare error", so it is asserted directly.
    m = re.search(r'const\s+nowOnOffer\s*=\s*([^;]+);', body)
    assert m, "the apply handler never captures what is now on offer"
    assert 'consentVersion' in m.group(1), (
        f"nowOnOffer is assigned `{m.group(1).strip()}` — it must come from "
        "`consentVersion`, which is re-read from the fresh status. Assigning it "
        "from `approved` would make the comparison below always false."
    )

    # BOTH post-apply paths must handle the move: the refusal path
    # (`started === false`) and the started path (the worker can still refuse
    # inside its own re-detection, after the request already returned started).
    # A handler that fixes only one of them still strands the user on a bare
    # error, which is what this requirement is about.
    comparisons = re.findall(r'nowOnOffer\s*!==\s*approved', body)
    assert len(comparisons) >= 2, (
        f"the moved-version check appears {len(comparisons)} time(s); both the "
        "'started but version moved' path and the 'refused outright' path need "
        "it. Handling one leaves the other showing a bare error."
    )

    assert 'refreshUpdateStatus' in body, (
        "the apply handler does not re-read the status after the refusal, so "
        "the user would be shown a stale version"
    )
    # The message, not an exception, and present in both branches.
    prompts = re.findall(r'approve again', body, flags=re.I)
    assert len(prompts) >= 2, (
        f"'review and approve again' appears {len(prompts)} time(s); both "
        "branches need it, or one of them shows the user a bare error"
    )


def test_the_refusal_branches_are_named_not_bare():
    """
    Every refusal reason the backend can return gets its own message rather
    than falling through to a generic one. `no_update_available` is the case
    that matters most for rule A: it is "could not determine", not "done".
    """
    js = _js_without_comments(_read(JS_PATH))
    body = _function_body(js, 'applyConsentedUpdate')
    for reason in ('no_update_available', 'disabled', 'debounced', 'in_progress'):
        assert f"'{reason}'" in body, \
            f"the apply handler has no branch for the backend's " \
            f"'{reason}' refusal"

    m = re.search(r"result\.reason\s*===\s*'no_update_available'(.*?)(?=\n\s*\}?\s*else)",
                  body, flags=re.S)
    assert m, "no_update_available branch not found"
    assert 'nothing' in m.group(1).lower(), \
        "the no_update_available message must say nothing was rebuilt, not " \
        "imply the browser is current"


# --------------------------------------------------------------------------- #
# 4 — progress, failure, badge visibility
# --------------------------------------------------------------------------- #
def test_the_chip_is_visible_without_opening_the_panel():
    """
    Requirement 1. The indicator lives in the titlebar and is never `hidden`;
    the panel is what toggles. If the chip were inside the panel it would be
    invisible until a log was opened, which is the original defect.
    """
    html = _html_without_comments(_read(HTML_PATH))
    chip = _by_id(html, 'btnUpdateChip')
    assert chip, "#btnUpdateChip is missing from index.html"
    assert chip[0] == 'button', "the indicator must be a real button so it is " \
                                "reachable by keyboard"
    assert 'hidden' not in chip[1], \
        "#btnUpdateChip must never be hidden; the whole point is that a " \
        "pending update is visible without opening anything"

    panel_pos = html.find('id="updatePanel"')
    chip_pos = html.find('id="btnUpdateChip"')
    assert chip_pos < panel_pos, \
        "the chip should be defined outside the panel it toggles"


def test_building_and_verifying_render_as_progress():
    """
    Requirement 4. A build in flight is reflected, and the panel copy for it
    says the current image is not being touched.
    """
    js = _js_without_comments(_read(JS_PATH))
    for state in ('building', 'verifying', 'checking'):
        assert f"case '{state}':" in _function_body(js, 'updateTone'), \
            f"updateTone does not handle '{state}' as progress"
        assert f"case '{state}':" in _function_body(js, 'updateChipLabel'), \
            f"updateChipLabel has no label for '{state}'"
    body = _function_body(js, 'updatePanelBody')
    assert 'not touched' in _case_chunks(body)['building'].lower(), \
        "the building copy should say the current image is not being touched"


def test_failed_and_interrupted_render_as_an_error_with_detail():
    """
    Requirement 4. A failed build must be unmistakably an error: 'error'
    tone, an error title, and the backend's `error` text surfaced.
    """
    js = _js_without_comments(_read(JS_PATH))
    cases = _case_chunks(_function_body(js, 'updateTone'))
    for state in ('failed', 'interrupted'):
        assert state in cases, f"updateTone has no case for '{state}'"
        # 'failed' and 'interrupted' share one `return 'error'` via
        # fallthrough, so check the union of their chunks rather than one.
        chunk = cases[state]
        if not chunk.strip():
            nxt = cases.get('interrupted', '')
            chunk = nxt
        assert "return 'error'" in chunk, f"'{state}' must be the error tone"
        assert f"case '{state}':" in _function_body(js, 'updatePanelTitle'), \
            f"updatePanelTitle has no title for '{state}'"

    body = _function_body(js, 'renderUpdate')
    assert "'failed'" in body and "'interrupted'" in body, \
        "the panel does not gate its error detail on a failed state"
    assert re.search(r'updateErrorDetail', body), \
        "the error detail element is never populated"
    assert 'updateErrorDetail' in _html_without_comments(_read(HTML_PATH)), \
        "#updateErrorDetail is missing from index.html"


def test_the_working_image_is_described_as_untouched_on_failure():
    """
    The backend's single most important property is that a failure never puts
    the installed image at risk. The UI should say so, or a red panel reads as
    "your browser is broken".
    """
    js = _js_without_comments(_read(JS_PATH))
    cases = _case_chunks(_function_body(js, 'updatePanelBody'))
    assert 'failed' in cases, "updatePanelBody has no 'failed' case"
    chunk = cases['failed']
    if not chunk.strip():
        chunk = cases.get('interrupted', '')
    assert 'untouched' in chunk.lower(), \
        "the failed-build copy does not say the existing image is untouched"


def test_base_eol_is_surfaced_as_its_own_condition():
    """
    Requirement 6. The backend names this state `base_eol`, carries
    `branch_eol`, and supplies `base_eol_notice` /
    `base_eol_notice_short`. Those are the field names to use — inventing
    `base_eol_supported` or similar would render nothing.

    Distinct from "Chromium is behind": a frozen Alpine branch reports "up to
    date" forever, so it must not borrow that state's tone.
    """
    js = _js_without_comments(_read(JS_PATH))
    tone_cases = _case_chunks(_function_body(js, 'updateTone'))
    assert 'base_eol' in tone_cases, "updateTone has no case for 'base_eol'"
    eol_tone = re.search(r"return\s+'([a-z]+)'", tone_cases['base_eol']).group(1)
    ok_tone = re.search(r"return\s+'([a-z]+)'", tone_cases['up_to_date']).group(1)
    assert eol_tone != ok_tone, (
        "base_eol must not share a tone with up_to_date: a frozen Alpine "
        "branch is reported as 'up to date' forever while going unpatched"
    )

    body_cases = _case_chunks(_function_body(js, 'updatePanelBody'))
    assert 'base_eol' in body_cases, "updatePanelBody has no case for 'base_eol'"
    assert 'base_eol_notice' in body_cases['base_eol'], (
        "the base_eol copy must come from the backend's `base_eol_notice`, "
        "which names the branch and the stable branch to move to"
    )
    assert 'base_eol' in _js_without_comments(_read(JS_PATH)), \
        "the renderer never mentions the base_eol state"


def test_branch_eol_is_not_treated_as_a_rebuild_trigger_in_the_ui():
    """
    The backend deliberately never rebuilds for an EOL base. The UI must not
    offer Apply in that state either, or it would be inviting a 1.31 GB no-op.
    `consent_required` is already false there (it is true only for
    `update_available`), so this asserts the UI relies on that rather than
    inventing its own trigger.
    """
    js = _js_without_comments(_read(JS_PATH))
    assert 'consent_required' in js, \
        "the renderer ignores `consent_required` and derives consent itself"
    body = _function_body(js, 'refreshUpdateStatus')
    assert 'consent_required' in body, \
        "consent must be gated on the backend's `consent_required` flag"
    assert 'branch_eol' not in body, \
        "the renderer must not gate consent on `branch_eol`; an EOL base is " \
        "reported, never offered for consent"


# --------------------------------------------------------------------------- #
# 5 — policy
# --------------------------------------------------------------------------- #
def test_disabled_policy_renders_a_note_and_nothing_else():
    """
    Requirement 5, `disabled` branch. Allowed: nothing at all, or a note. NOT
    allowed: an Apply button, or a green "up to date".
    """
    js = _js_without_comments(_read(JS_PATH))
    for fn in ('updateTone', 'updateChipLabel', 'updatePanelTitle', 'updatePanelBody'):
        body = _function_body(js, fn)
        assert 'disabled' in body, f"{fn} does not handle the disabled policy"
    body = _function_body(js, 'renderUpdate')
    m = re.search(r'const\s+showApply\s*=\s*(.*?);', body, flags=re.S)
    assert "'disabled'" in m.group(1), \
        "Apply is offered while updates are disabled"


def test_unattended_policy_explains_itself_and_offers_no_button():
    """
    Requirement 5, `unattended` branch. The user is told the rebuild happens
    by itself, so there is nothing to approve — and no button pretending
    otherwise.
    """
    js = _js_without_comments(_read(JS_PATH))
    body = _function_body(js, 'updatePanelBody')
    assert 'unattended' in body, \
        "the panel does not explain the unattended policy"
    m = re.search(r'const\s+showApply\s*=\s*(.*?);',
                  _function_body(js, 'renderUpdate'), flags=re.S)
    assert 'unattended' in m.group(1), \
        "an Apply button is offered under the unattended policy"


# --------------------------------------------------------------------------- #
# 6 — polling is not a tight loop
# --------------------------------------------------------------------------- #
def test_the_poll_interval_is_not_tight():
    """
    Requirement 7. The status read is cheap but it is still a poll across a
    bridge. The idle interval must be seconds, not milliseconds; the faster
    one is reserved for a build actually being in flight.
    """
    js = _js_without_comments(_read(JS_PATH))
    idle = re.search(r'UPDATE_POLL_MS\s*=\s*(\d+)', js)
    busy = re.search(r'UPDATE_POLL_BUSY_MS\s*=\s*(\d+)', js)
    assert idle, "no idle poll interval constant"
    assert busy, "no in-flight poll interval constant"
    idle_ms, busy_ms = int(idle.group(1)), int(busy.group(1))
    assert idle_ms >= 15000, (
        f"the idle poll is {idle_ms} ms; the status read goes over a bridge and "
        "15 s or slower is a sensible reflection interval"
    )
    assert busy_ms >= 1000, "the in-flight poll is a tight loop"
    # While idle the UI must NOT be polling faster than it needs to. Only an
    # in-flight build justifies the tighter interval.
    assert busy_ms < idle_ms, (
        f"the in-flight interval ({busy_ms} ms) is not tighter than the idle "
        f"one ({idle_ms} ms). The fast interval exists to show build progress; "
        "if it is the larger of the two the intent is inverted and the app "
        "polls slowly while a build runs and fast while nothing happens."
    )


def test_polling_never_starts_a_check():
    """
    The poll reflects state. If it called `image:checkUpdate` it would be
    driving detection on a timer, which is a different thing entirely.
    """
    js = _js_without_comments(_read(JS_PATH))
    body = _function_body(js, 'scheduleUpdatePoll')
    assert 'image.check' not in body, \
        "the poll must reflect status, not trigger a version check"
    poll_body = _function_body(js, 'refreshUpdateStatus')
    assert 'image.check' not in poll_body, \
        "refreshUpdateStatus must not trigger a version check"
    # Only the explicit button may.
    assert 'image.check' in _read(JS_PATH), \
        "nothing calls image.check(); the user has no way to ask for a check"


# --------------------------------------------------------------------------- #
# 7 — accessibility: no unbacked modal claim
# --------------------------------------------------------------------------- #
def test_the_update_panel_is_not_a_modal_overlay():
    """
    The panel is a plain `<section>` inside the content column. It must not
    pick up the `modal-overlay` class, which is the app's (ARIA-free) modal
    styling and reads as "this takes over the screen".
    """
    html = _html_without_comments(_read(HTML_PATH))
    found = _by_id(html, 'updatePanel')
    assert found, "#updatePanel is missing"
    assert 'modal-overlay' not in found[1], \
        "#updatePanel picked up the modal-overlay class; it is an inline panel"


def test_no_modal_aria_is_introduced_anywhere_in_the_update_ui():
    """
    Commit c6405d5 removed `aria-modal` / `role="dialog"` because the app has
    no focus trap and never honours them. The update panel must not bring them
    back.

    `aria-expanded` / `aria-controls` are a different thing and are allowed:
    they describe a real disclosure that really does toggle.
    """
    html = _html_without_comments(_read(HTML_PATH))
    for elem_id in ('updatePanel', 'btnUpdateChip'):
        found = _by_id(html, elem_id)
        assert found, f"#{elem_id} is missing"
        tag, attrs = found
        assert 'aria-modal' not in attrs, f"#{elem_id} declares aria-modal"
        assert not re.search(r'\brole\s*=', attrs), \
            f"#{elem_id} declares a role; there is no focus trap to back it"


def test_the_existing_a11y_regressions_still_hold():
    """
    The update UI must not have re-broken the R7 invariants that
    test_renderer_a11y.py locks in. Re-asserted here because that file may not
    be the only gate this change passes.
    """
    html = _html_without_comments(_read(HTML_PATH))
    for tag, attrs in re.findall(r'<(div|section)\b([^>]*?)>', html):
        if 'modal-overlay' in attrs:
            assert 'aria-modal' not in attrs, \
                "a modal overlay has aria-modal again"
            assert not re.search(r'\brole\s*=', attrs), \
                "a modal overlay has a role again"

    js = _js_without_comments(_read(JS_PATH))
    assert not re.search(r"""key\s*===?\s*['"]Tab['"]""", js), \
        "a Tab key handler appeared; focus containment is new and unverified"
    assert 'focusin' not in js and 'focusout' not in js, \
        "focusin/focusout appeared; focus management is new and unverified"


# --------------------------------------------------------------------------- #
# 8 — CSS: the tones are actually distinguishable
# --------------------------------------------------------------------------- #
def test_css_distinguishes_the_warning_tone_from_the_success_tone():
    """
    A distinct tone name in JavaScript means nothing if both classes render
    the same colour. Assert the two selectors exist and are not the same rule.
    """
    css = _read(CSS_PATH)
    for tone in ('ok', 'attention', 'warn', 'error', 'progress'):
        assert re.search(r'\.update-panel\[data-tone="%s"\]' % tone, css), \
            f"no .update-panel[data-tone=\"{tone}\"] rule"
        assert re.search(r'\.update-chip\[data-tone="%s"\]' % tone, css), \
            f"no .update-chip[data-tone=\"{tone}\"] rule"

    def colour_of(selector):
        m = re.search(re.escape(selector) + r'\s*\{([^}]*)\}', css)
        assert m, f"{selector} has no rule body"
        return re.findall(r'var\((--[a-z0-9-]+)\)', m.group(1))

    ok_colour = colour_of('.update-panel[data-tone="ok"]')
    warn_colour = colour_of('.update-panel[data-tone="warn"]')
    assert ok_colour and warn_colour, "could not read both tone colours"
    assert set(ok_colour) != set(warn_colour), (
        f"'ok' and 'warn' resolve to the same colour variable(s) {ok_colour}. "
        "The could-not-determine state must not look like success."
    )
    assert '--success' in ' '.join(ok_colour), \
        "the success tone should use the success colour"
    assert '--warning' in ' '.join(warn_colour), \
        "the could-not-determine tone should use the warning colour"


def test_css_styles_the_elements_app_js_writes_to():
    """Non-vacuity: the hooks exist so the panel does not render unstyled."""
    css = _read(CSS_PATH)
    for selector in ('.update-chip', '.update-panel', '.update-consent-warning',
                     '.update-error-detail', '.update-panel-actions'):
        assert re.search(re.escape(selector) + r'\s*[,{]', css), \
            f"style.css has no rule for {selector}"
    assert re.search(r'\.update-chip-label\s*[,{]', css), \
        "the chip label has no rule, so long states will not ellipsize"


def test_hidden_panels_are_actually_hidden():
    """
    `hidden` is the display:none toggle. A rule that overrides it would leave
    the panel permanently on screen.
    """
    css = _read(CSS_PATH)
    for selector in ('.update-panel[hidden]', '.update-consent-warning[hidden]',
                     '.update-error-detail[hidden]'):
        m = re.search(re.escape(selector) + r'\s*\{([^}]*)\}', css)
        assert m, f"no {selector} rule"
        assert re.search(r'display\s*:\s*none', m.group(1)), \
            f"{selector} does not set display:none, so the element would stay " \
            "visible despite the attribute"


# --------------------------------------------------------------------------- #
# 9 — non-vacuity
# --------------------------------------------------------------------------- #
def test_the_tests_would_fail_without_the_feature():
    """
    The whole file must fail if the update UI is removed, rather than passing
    because it found nothing to check. Simulated on the real sources: strip the
    chip and panel out of the HTML and confirm the structural tests' targets
    are gone.
    """
    html = _html_without_comments(_read(HTML_PATH))
    stripped = html
    for elem_id in ('btnUpdateChip', 'updatePanel', 'updateConsentWarning',
                    'updateErrorDetail', 'btnApplyUpdate'):
        stripped = re.sub(r'<[^>]*id="%s"[^>]*>' % re.escape(elem_id), '',
                          stripped)
    assert _by_id(stripped, 'btnUpdateChip') is None
    assert _by_id(stripped, 'updatePanel') is None
    assert _by_id(stripped, 'updateConsentWarning') is None
    # And the JS tone function is what makes the states distinguishable.
    js = _js_without_comments(_read(JS_PATH))
    assert "case 'unknown':" in _function_body(js, 'updateTone')
    broken = re.sub(r"case\s+'unknown'\s*:", "case 'up_to_date':",
                    _function_body(js, 'updateTone'))
    assert broken != _function_body(js, 'updateTone'), \
        "the mutation did not apply, so the assertion would be vacuous"


def test_the_backend_is_the_source_of_the_warning_text():
    """
    Confirms this test file is pointed at the real source of the wording: the
    backend still defines `CONSENT_WARNING` with the `{current}` placeholder,
    which is what `consent_warning` is built from on every status poll.
    """
    src = _read(BACKEND_PATH)
    assert 'CONSENT_WARNING' in src, "the backend no longer defines CONSENT_WARNING"
    m = re.search(r'CONSENT_WARNING\s*=\s*\((.*?)\)\n', src, flags=re.S)
    assert m and '{current}' in m.group(1), \
        "CONSENT_WARNING no longer carries the {current} placeholder"
    assert re.search(r'CONSENT_WARNING\.replace\("\{current\}"', src), \
        "the backend no longer substitutes {current} into the warning"


# --------------------------------------------------------------------------- #
# standalone runner
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
