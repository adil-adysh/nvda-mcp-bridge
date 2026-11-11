#!/usr/bin/env python3
"""Test the NVDA HTTP bridge execute_python action."""

import json
import sys
from pathlib import Path
import urllib.request
from urllib.error import HTTPError, URLError

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bridge_auth import auth_headers  # noqa: E402

BASE_URL = "http://127.0.0.1:8765"
HEADERS = auth_headers()

CODE = "x = 1 + 1\nprint('hello')\nx"


def request(path, body):
    url = BASE_URL + path
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    for key, value in HEADERS.items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            text = resp.read().decode("utf-8", errors="replace")
            try:
                return resp.status, json.loads(text)
            except json.JSONDecodeError:
                return resp.status, text
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return exc.code, {"error": body}
    except URLError as exc:
        return None, {"error": str(exc)}
    except Exception as exc:
        return None, {"error": str(exc)}


def main():
    payload = {
        "request_id": "exec-python-test-1",
        "action": "execute_python",
        "params": {
            "code": CODE,
            "snapshot_vars": False,
            "capture_vars": ["x"],
        },
    }
    status, body = request("/action", payload)
    print("Status:", status)
    print(json.dumps(body, indent=2, ensure_ascii=False))
    return 0 if status == 200 and isinstance(body, dict) and body.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
