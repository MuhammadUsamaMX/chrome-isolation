#!/usr/bin/env python3
"""Renderer ARIA honesty + modal-dismissal regressions (review finding R7).

An independent review found that the renderer *added* ARIA that overstates what
the app actually does. Every test here is written against the fixed behaviour
and is deliberately not weakened to match the overstatement.

The specific defect, as verified rather than assumed:

  A  `role="dialog" aria-modal="true"` on 4 modal overlays with NO focus trap.
     The only `keydown` handler in app.js is Escape. `aria-modal="true"` is an
     instruction to assistive tech that content outside the dialog is inert; it
     is not, and nothing enforces it. Tab walks straight out of an open dialog
     into the profile cards behind it and reaches the Delete button.
  B  Focus is never moved into the Edit or Proxies modals, and never restored
     on close — so `aria-modal` was claiming a dialog contract the app did not
     implement.
  C  `readonly` on the three path inputs removed the ability to type or paste a
     path. This is a FUNCTIONAL regression for everyone, not only screen-reader
     users: Browse became the sole route. Fix is to drop `readonly`.
  D  `<button>` nested inside `<label>` (invalid HTML in both modals) gave one
     click two possible meanings.
  E  `closeAllModals()` called `form.reset()`, so one stray Escape silently
     discarded a half-typed profile with no undo.

The renderer has no browser test harness and these tests must not introduce one,
so everything here is a static assertion over the two source files. See the
bottom of this file for the revert-the-fix proof that these tests are not
vacuous.
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


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _read(path):
    with open(path, encoding='utf-8') as fh:
        return fh.read()


def _html_without_comments(src):
    """
    index.html with `<!-- ... -->` blocks removed.

    The R7 comments in the file deliberately spell out the attributes that were
    removed ("this overlay carries NO role=\"dialog\" / aria-modal / ...") so a
    future reader does not helpfully re-add them. A naive substring grep matches
    that explanation and fails a correct implementation, exactly as the
    desktop_manager docstrings hit the same trap in test_desktop_identity.py.
    """
    return re.sub(r'<!--.*?-->', '', src, flags=re.S)


def _js_without_comments(src):
    """app.js with `//` line comments removed, string literals kept.

    A `#` inside a string is not a comment in JS, but neither is one inside a
    URL, so this only strips `//` to end-of-line — which is what app.js uses.
    Block comments are stripped too.
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


def _open_tags(src, tag):
    """Every `<tag ...>` start tag, in document order."""
    return re.findall(r'<%s\b[^>]*>' % tag, src, flags=re.I)


def _elements(src):
    """
    Minimal element index: [(tag, attrs_text)] for every start tag.

    Enough to answer "which element carries id=X" and "what attributes does it
    have" without pulling in a parser or a browser.
    """
    out = []
    for m in re.finditer(r'<([a-zA-Z][a-zA-Z0-9-]*)\b([^>]*?)/?>', src):
        out.append((m.group(1).lower(), m.group(2)))
    return out


def _by_id(src, elem_id):
    """The (tag, attrs_text) whose id attribute equals elem_id, or None."""
    for tag, attrs in _elements(src):
        if re.search(r'\bid\s*=\s*"%s"' % re.escape(elem_id), attrs):
            return tag, attrs
    return None


def _modal_overlays(html):
    return [a for t, a in _elements(html) if t == 'div' and 'class="modal-overlay"' in a]


# --------------------------------------------------------------------------- #
# A/B — no ARIA that promises a modal dialog the app does not implement
# --------------------------------------------------------------------------- #
def test_no_modal_overlay_declares_aria_modal():
    """
    The core regression. `aria-modal="true"` is a behavioural claim, not a
    label: it tells a screen reader the rest of the page is inert. This app
    traps no focus, so the claim is false and a Tab key leaves the dialog
    toward a Delete button.
    """
    html = _html_without_comments(_read(HTML_PATH))
    offenders = [a for a in _modal_overlays(html) if 'aria-modal' in a]
    assert not offenders, (
        f"{len(offenders)} modal overlay(s) declare aria-modal: {offenders}\n"
        "The renderer has no focus trap — the only keydown handler is Escape — "
        "so this claims a dialog the app does not implement. Remove it, or ship "
        "a real focus trap first and then re-add it with a test that proves the "
        "trap exists."
    )


def test_no_modal_overlay_declares_role_dialog():
    """
    `role="dialog"` without a focus trap and without focus management (never
    moved in on open, never restored on close) is the same overstatement in a
    different attribute. It was added in the same change as aria-modal.
    """
    html = _html_without_comments(_read(HTML_PATH))
    offenders = [a for a in _modal_overlays(html) if re.search(r'\brole\s*=', a)]
    assert not offenders, (
        f"modal overlay(s) still declare a role: {offenders}\n"
        "Focus is never moved into the Edit/Proxies modals and never restored "
        "on close, so role=\"dialog\" is an unimplemented contract."
    )


def test_no_aria_labelledby_points_at_a_modal_title():
    """
    `aria-labelledby` was part of the same dialog-role claim — it exists only
    to name a role="dialog" box. The headings keep their ids (app.js writes to
    `confirmTitle` and `editTitle`), so this only removes the dangling
    reference, not the element.
    """
    html = _html_without_comments(_read(HTML_PATH))
    offenders = [a for a in _modal_overlays(html) if 'aria-labelledby' in a]
    assert not offenders, (
        f"modal overlay(s) still declare aria-labelledby: {offenders}\n"
        "This names a role=\"dialog\" box; it goes with the role attribute."
    )
    # The title elements app.js depends on must still exist.
    for title_id in ('createTitle', 'editTitle', 'proxiesTitle', 'confirmTitle'):
        assert _by_id(html, title_id), \
            f"#{title_id} was removed but app.js still writes to it"


def test_the_renderer_has_no_focus_trap():
    """
    States the precondition for the three tests above, in the form a future
    change can act on. If this ever becomes false — someone adds a real trap —
    the ARIA attributes are no longer overstatements and may return. The
    current trap: a keydown handler on Tab, or a sentinelled focusin listener,
    inside a modal.
    """
    js = _js_without_comments(_read(JS_PATH))
    has_tab_trap = re.search(r"""key\s*===?\s*['"]Tab['"]""", js) is not None
    has_focusin = 'focusin' in js or 'focusout' in js
    has_inert = "'inert'" in js or '"inert"' in js
    assert not (has_tab_trap or has_focusin or has_inert), (
        "the renderer now appears to implement focus containment "
        f"(Tab trap={has_tab_trap}, focusin/focusout={has_focusin}, "
        f"inert={has_inert}). Focus management and focus restoration may be "
        "incomplete still — check before re-adding role=\"dialog\"/aria-modal, "
        "and update this test to assert the trap's behaviour, not its absence."
    )


# --------------------------------------------------------------------------- #
# The aria attributes that ARE true must survive
# --------------------------------------------------------------------------- #
def test_decorative_svgs_are_still_aria_hidden():
    """
    Non-vacuity. `aria-hidden="true"` on a decorative SVG is a true statement
    about the present markup, costs nothing, and does not imply any behaviour.
    The revert must not have swept it up.
    """
    html = _read(HTML_PATH)
    hidden = [a for t, a in _elements(html)
              if t == 'svg' and 'aria-hidden="true"' in a]
    assert len(hidden) >= 4, (
        f"only {len(hidden)} svg(s) carry aria-hidden=\"true\"; the revert must "
        "keep the honest ones — a decorative icon inside a labelled button is "
        "noise for a screen reader, not a false claim about the app"
    )


def test_icon_only_buttons_keep_an_aria_label():
    """
    Non-vacuity. The window-control and clear buttons have no text content, so
    `aria-label` is the only thing naming them. It is a true statement about the
    markup and was never part of the dialog-role overstatement.
    """
    html = _read(HTML_PATH)
    for btn_id in ('btnWinMin', 'btnWinMax', 'btnWinClose', 'btnTheme',
                   'btnRefresh', 'btnClearPath', 'btnClearCreate', 'btnClearEdit'):
        found = _by_id(html, btn_id)
        assert found, f"#{btn_id} is gone"
        tag, attrs = found
        assert tag == 'button', f"#{btn_id} should be a button, got <{tag}>"
        assert 'aria-label=' in attrs, (
            f"#{btn_id} has no text content and no aria-label; it is now an "
            "unnamed control"
        )


# --------------------------------------------------------------------------- #
# C — the path inputs must not be readonly
# --------------------------------------------------------------------------- #
PATH_INPUT_IDS = ('inputPath', 'inputHostMount', 'editHostMount')


def test_path_inputs_are_not_readonly():
    """
    `readonly` was a functional regression, not just an a11y one: it removed
    typing and pasting a path for EVERY user, not only screen-reader users,
    leaving the native Browse dialog as the sole route. Typing a path is a real
    capability — long paths, remote mounts and paths from the clipboard are all
    easier typed than picked.
    """
    html = _html_without_comments(_read(HTML_PATH))
    for input_id in PATH_INPUT_IDS:
        found = _by_id(html, input_id)
        assert found, f"#{input_id} is missing from index.html"
        tag, attrs = found
        assert tag == 'input', f"#{input_id} should be an <input>, got <{tag}>"
        assert 'type="text"' in attrs, f"#{input_id} should stay a text input"
        assert not re.search(r'\breadonly\b', attrs), (
            f"#{input_id} is readonly again: typing and pasting a path is a "
            "supported capability, and Browse is a convenience beside it, not "
            "the only way in. See the R7 note in index.html."
        )


def test_readonly_does_not_appear_on_any_modal_input():
    """
    Broader sweep: no path-ish field in any modal may reintroduce it. Catches
    a new field that copies the old pattern.
    """
    html = _html_without_comments(_read(HTML_PATH))
    offenders = []
    for tag, attrs in _elements(html):
        if tag != 'input':
            continue
        if re.search(r'\breadonly\b', attrs):
            offenders.append(attrs.strip())
    assert not offenders, f"readonly input(s) in the renderer: {offenders}"


def test_each_path_input_has_a_browse_button():
    """
    The non-vacuity half of dropping `readonly`: the inputs lost their
    click-to-open-the-picker behaviour (a click on a text field must place the
    caret, not launch a native dialog), so the Browse button is now the single
    picker affordance. It must exist for all three fields.
    """
    html = _read(HTML_PATH)
    for btn_id, input_id in (('btnBrowsePath', 'inputPath'),
                             ('btnBrowseCreate', 'inputHostMount'),
                             ('btnBrowseEdit', 'editHostMount')):
        assert _by_id(html, btn_id), f"#{btn_id} (the picker for #{input_id}) is gone"
        assert btn_id in _read(JS_PATH), \
            f"#{btn_id} is in the markup but app.js never wires it to browseFolder()"


def test_app_js_does_not_open_the_picker_on_a_click_of_the_input():
    """
    With the inputs editable again, a click on the field is a text-edit click.
    Binding browseFolder() to the input's own click would fight the caret and
    was only ever coherent while the field was readonly.
    """
    js = _js_without_comments(_read(JS_PATH))
    for input_id in PATH_INPUT_IDS:
        # Scoped to ONE statement: `[^;]*?` cannot cross a semicolon, so a
        # click handler on some *other* element later in the file is not a
        # false positive.
        direct = re.compile(
            r"getElementById\('%s'\)[^;]*?addEventListener\(\s*'click'" % input_id)
        assert not direct.search(js), (
            f"app.js binds a click handler directly to #{input_id}; with the "
            "field editable again, a click on it must place the caret. Browse "
            "belongs on the Browse button only."
        )
    # The old code was a loop, not a direct binding:
    #   ['inputPath','inputHostMount','editHostMount'].forEach(id => {
    #     const el = getElementById(id);
    #     if (el) el.addEventListener('click', () => browseFolder(id));
    #   });
    # That array literal existed for no purpose other than binding the click
    # handler, so its reappearance is the signal. Matching the array rather than
    # trying to regex across the arrow body's parentheses is both simpler and
    # exact.
    arr = re.compile(
        r"\[[^\]]*\b(?:%s)\b[^\]]*\]\s*\.forEach" % '|'.join(PATH_INPUT_IDS))
    assert not arr.search(js), (
        "app.js again iterates the path inputs (%s) and binds a click handler "
        "to each; browseFolder must be bound to the Browse buttons so a click "
        "on an editable field can place the caret" % arr.search(js).group(0)
    )


# --------------------------------------------------------------------------- #
# D — no <button> inside <label>
# --------------------------------------------------------------------------- #
def test_no_button_or_select_is_nested_inside_a_label():
    """
    `<label>` may contain phrasing content and at most one labelable
    descendant. A `<button>` inside one is invalid HTML, and a click inside
    that button activates BOTH the label's control and the button — ambiguous
    to a pointer and worse for a screen reader, which announces one control
    while the click hits another.
    """
    html = _html_without_comments(_read(HTML_PATH))
    stack = []
    offenders = []
    for m in re.finditer(r'<(/?)([a-zA-Z][a-zA-Z0-9-]*)\b[^>]*?(/?)>', html):
        closing, tag, self_closing = m.group(1), m.group(2).lower(), m.group(3)
        if self_closing:
            if any(t == 'label' for t in stack) and tag in ('button', 'select', 'input'):
                offenders.append(f"<{tag}> inside <label>")
            continue
        if closing:
            while stack and stack.pop() != tag:
                pass
        else:
            if tag == 'label' or any(t == 'label' for t in stack):
                if tag in ('button', 'select', 'input'):
                    offenders.append(f"<{tag}> inside <label>")
            stack.append(tag)
    assert not offenders, (
        f"labelable/interactive element nested in a <label>: {sorted(set(offenders))}\n"
        "Use a .field wrapper with <label for=\"...\"> naming exactly one "
        "control, and keep the buttons outside the label."
    )


def test_every_form_control_has_an_explicit_label_for():
    """
    The positive half of the same fix: after unwrapping, each control must be
    named by a `for` attribute, not by accident of nesting. The custom-proxy
    inputs and the proxy-add inputs are named by aria-label instead — they are
    secondary fields with no visible label — and the generic HTML `placeholder`
    is explicitly NOT accepted here, because a placeholder is not an accessible
    name and disappears as soon as the user types.
    """
    html = _html_without_comments(_read(HTML_PATH))
    labels = set()
    for m in re.finditer(r'<label\b[^>]*\bfor\s*=\s*"([^"]+)"', html):
        labels.add(m.group(1))
    assert labels, "no <label for=...> at all — the fix unwrapped labels to nothing"

    unlabelled = []
    for tag, attrs in _elements(html):
        if tag not in ('input', 'select', 'textarea'):
            continue
        m = re.search(r'\bid\s*=\s*"([^"]+)"', attrs)
        if not m:
            continue
        elem_id = m.group(1)
        if elem_id in labels:
            continue
        if 'aria-label=' in attrs:
            continue
        unlabelled.append(elem_id)
    assert not unlabelled, (
        f"form control(s) with neither <label for> nor aria-label: {unlabelled}\n"
        "A placeholder is not an accessible name."
    )


# --------------------------------------------------------------------------- #
# E — Escape must not discard a half-typed Create form
# --------------------------------------------------------------------------- #
def test_close_all_modals_does_not_reset_the_create_form():
    """
    The discard bug. `closeAllModals()` runs on Escape and on an overlay click
    — the two easiest keys/gestures to trigger by accident — and it called
    form.reset() on the Create modal. One stray Escape silently threw away a
    half-typed profile, with no confirmation and no undo.
    """
    js = _js_without_comments(_read(JS_PATH))
    m = re.search(r'function closeAllModals\(\)\s*\{', js)
    assert m, "closeAllModals() is gone from app.js"
    # Take the function body by brace balance. Comments are already stripped —
    # the explanatory comment in the function names form.reset() precisely to
    # say it is NOT called there, and a naive grep would match that sentence.
    start = m.end()
    depth, i = 1, start
    while depth and i < len(js):
        if js[i] == '{':
            depth += 1
        elif js[i] == '}':
            depth -= 1
        i += 1
    body = js[start:i - 1]
    assert not re.search(r'\.reset\s*\(', body), (
        "closeAllModals() calls .reset() again. Escape / overlay-click "
        "dismisses a modal; it must not clear the Create form. Clearing belongs "
        "on the two unambiguous paths: a successful submit and the explicit "
        f"Cancel button.\n{body}"
    )


def test_reset_is_reachable_only_from_submit_and_cancel():
    """
    Belt and braces on the Create form: it may be cleared from exactly two
    places — a successful submit and the explicit Cancel button. A third would
    be a new silent-discard path.

    `proxyForm.reset()` is excluded: the proxy manager's Add form is cleared
    after a successful add, which is unambiguous in the same way and is not
    what this finding is about.
    """
    js = _js_without_comments(_read(JS_PATH))
    create_resets = [line.strip() for line in js.splitlines()
                     if re.search(r'\.reset\s*\(', line) and 'createForm' in line]
    assert len(create_resets) == 2, (
        f"expected exactly 2 createForm .reset() calls (submit + Cancel), found "
        f"{len(create_resets)}: {create_resets}"
    )
    # And nothing resets the Create form under any other spelling.
    all_resets = [line.strip() for line in js.splitlines()
                  if re.search(r'\.reset\s*\(', line)]
    assert all('createForm' in line or 'proxyForm' in line for line in all_resets), (
        f"a .reset() on an unexpected form appeared: {all_resets}"
    )


def test_cancel_button_still_clears_the_create_form():
    """
    Non-vacuity: preserving the form on Escape does not mean the form is never
    cleared. The explicitly-labelled Cancel button is the user's stated
    "throw this away", and it must still clear, or a stale half-filled form
    would greet the next person to open it.
    """
    js = _read(JS_PATH)
    m = re.search(r"btnCreateCancel'\)", js)
    assert m, "the Cancel button has no handler"
    handler = js[m.start():m.start() + 500]
    assert re.search(r'createForm[\s\S]{0,80}?\.reset\s*\(', handler), (
        "btnCreateCancel no longer clears the Create form; an explicit Cancel "
        "is the one dismissal where discarding the work is what the user asked "
        "for"
    )


# --------------------------------------------------------------------------- #
# Structural sanity — the revert must not have deleted the modals
# --------------------------------------------------------------------------- #
def test_all_four_modal_overlays_are_still_present():
    """Non-vacuity: an earlier fix could 'pass' by removing the dialogs."""
    html = _html_without_comments(_read(HTML_PATH))
    for modal_id in ('modalCreate', 'modalEdit', 'modalProxies', 'modalConfirm'):
        found = _by_id(html, modal_id)
        assert found, f"#{modal_id} is missing from index.html"
        assert 'modal-overlay' in found[1], \
            f"#{modal_id} lost its modal-overlay class"


def test_css_still_styles_the_new_field_wrapper():
    """
    The label/field restructure changed the CSS contract: `.modal label` used
    to be the flex column that held the input. It is now `.modal .field`. If
    that rule were missing the modals would still work but render unstyled.
    """
    css = _read(CSS_PATH)
    assert re.search(r'\.modal\s+\.field\s*\{', css), (
        "style.css has no `.modal .field` rule; the label/field restructure "
        "left the modal layout unstyled"
    )
    assert re.search(r'\.input-row\s*\{', css), \
        "style.css lost its .input-row rule"


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
