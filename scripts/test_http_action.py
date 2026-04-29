#!/usr/bin/env python3
"""Test only the /action endpoint on the running NVDA HTTP bridge."""

import json
import urllib.request
from urllib.error import HTTPError, URLError

BASE_URL = "http://127.0.0.1:8765"
HEADERS = {
    "X-NVDA-Bridge-Token": "nvda-bridge-secret-change-me",
    "Content-Type": "application/json",
}

def main() -> int:
    body = json.dumps({
        "request_id": "http-test-action-1",
        "action": "get_current_focus",
        "params": {},
    }).encode("utf-8")
    req = urllib.request.Request(f"{BASE_URL}/action", data=body, method="POST")
    for key, value in HEADERS.items():
        req.add_header(key, value)

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            text = resp.read().decode("utf-8", errors="replace")
            print(resp.status)
            print(text)
            return 0
    except HTTPError as exc:
        print(exc.code)
        print(exc.read().decode("utf-8", errors="replace"))
        return 1
    except URLError as exc:
        print("URL error:", exc)
        return 1

if __name__ == "__main__":
    raise SystemExit(main())
