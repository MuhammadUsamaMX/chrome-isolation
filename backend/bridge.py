#!/usr/bin/env python3
"""
Chrome Isolation — stdio JSON bridge.
Electron main process spawns this as a child process.
Reads newline-delimited JSON requests from stdin, writes JSON responses to stdout.
All errors are returned as {"error": "..."} — never as exceptions.

Protocol:
  Request:  {"id": "<string>", "method": "<name>", "params": {...}}
  Response: {"id": "<string>", "result": ...}  or  {"id": "<string>", "error": "..."}
"""
import json
import sys
import traceback

# Ensure backend modules are importable
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import profile_service as svc
import proxy_store
import image_update

#: Set once the version detector has been handed to `image_update`. Guards the
#: lazy import in `_ensure_version_detector`; not a correctness flag.
_detector_registered = False


_METHODS = {
    "list_profiles":   lambda p: svc.get_all_profiles(),
    # Re-derive our .desktop entries. Idempotent and cheap; run at startup so
    # entries written by older builds self-heal.
    "refresh_desktop_entries": lambda p: svc.refresh_desktop_entries(),
    "create_profile":  lambda p: svc.create_profile(p["name"], p.get("custom_path", ""), p.get("host_mount", ""), p.get("proxy", "")),
    "update_profile":  lambda p: svc.update_profile(p["name"], p.get("host_mount"), p.get("proxy"), p.get("timezone")),
    "delete_profile":  lambda p: svc.delete_profile(p["name"]),
    "start_profile":   lambda p: svc.start_profile(p["name"]),
    "stop_profile":    lambda p: svc.stop_profile(p["name"]),
    "profile_status":  lambda p: svc.profile_status(p["name"]),
    "import_profile":  lambda p: svc.import_profile(p["archive_path"]),
    "export_profile":  lambda p: svc.export_profile(p["name"], p["dest_path"]),
    "list_proxies":    lambda p: proxy_store.list_proxies(),
    "add_proxy":       lambda p: proxy_store.add_proxy(p["name"], p["url"]),
    "delete_proxy":    lambda p: proxy_store.delete_proxy(p["name"]),

    # Chromium update — DETECT AND INFORM, REBUILD ONLY ON CONSENT.
    #
    # `image_update_check` runs a version check on a background thread and
    # RETURNS IMMEDIATELY. Under the default policy it BUILDS NOTHING: it
    # records `update_available` (with current + latest) and stops. A rebuild
    # requires `image_update_apply`, which a human invokes.
    #
    # `image_update_apply` is the consent gate. `version` must match the
    # version the status reports as available — consent is tied to a specific
    # version, never a blanket "yes to updates". If the version moved between
    # the check and the click, it builds nothing and the UI must ask again.
    #
    # `image_update_status` is a cheap in-memory read for the UI to poll.
    # None of the three touches Docker on the request path.
    "image_update_check":  lambda p: _ensure_version_detector() or image_update.check_and_start(
        reason=p.get("reason", "manual")),
    "image_update_apply":  lambda p: image_update.apply_update(
        version=p.get("version"), reason="user_consent"),
    "image_update_status": lambda p: image_update.get_update_status(),
}


def _ensure_version_detector():
    """
    Install the Chromium version detector into `image_update` on first use.

    Without this, `image_update` has no detector, its default is fail-closed,
    and every check reports "update availability unknown" -- so the app can
    never discover an update and the feature silently does nothing. The
    detector module is self-registering via the
    CHROME_ISOLATION_VERSION_DETECTOR env var, but nothing in the shipped
    launcher sets that variable, so the default installation had no detector at
    all.

    Deliberately lazy: importing `chromium_version` at module scope would put
    it on the backend's import path, and the brief for that module is explicit
    that no network or heavy work happens at import. Doing it on the first
    update call keeps startup untouched. Idempotent and never raises -- a
    failure here must degrade to "cannot check", not to a broken bridge.
    """
    global _detector_registered
    if _detector_registered:
        return
    _detector_registered = True
    try:
        import chromium_version
        chromium_version.register_detector(image_update)
    except Exception as exc:  # pragma: no cover - defensive
        print(f"[bridge] version detector unavailable: {exc}", file=sys.stderr)


def _handle(req: dict) -> dict:
    req_id = req.get("id", "")
    method = req.get("method", "")
    params = req.get("params", {})

    if method not in _METHODS:
        return {"id": req_id, "error": f"Unknown method: {method}"}

    try:
        result = _METHODS[method](params)
        return {"id": req_id, "result": result}
    except (ValueError, FileNotFoundError) as e:
        return {"id": req_id, "error": str(e)}
    except Exception as e:
        # Full traceback to stderr for dev debugging (npm start 2>&1);
        # the client only gets a clean, non-leaky message.
        traceback.print_exc()
        return {"id": req_id, "error": f"Internal error: {e}"}


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as e:
            resp = {"id": "", "error": f"Invalid JSON: {e}"}
        else:
            resp = _handle(req)

        sys.stdout.write(json.dumps(resp) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
