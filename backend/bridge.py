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


_METHODS = {
    "list_profiles":   lambda p: svc.get_all_profiles(),
    "create_profile":  lambda p: svc.create_profile(p["name"], p.get("custom_path", "")),
    "delete_profile":  lambda p: svc.delete_profile(p["name"]),
    "start_profile":   lambda p: svc.start_profile(p["name"]),
    "stop_profile":    lambda p: svc.stop_profile(p["name"]),
    "profile_status":  lambda p: svc.profile_status(p["name"]),
    "import_profile":  lambda p: svc.import_profile(p["archive_path"]),
    "export_profile":  lambda p: svc.export_profile(p["name"], p["dest_path"]),
}


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
        return {"id": req_id, "error": f"Internal error: {traceback.format_exc()}"}


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
