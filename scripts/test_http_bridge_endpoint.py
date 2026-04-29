#!/usr/bin/env python3
"""Test the running NVDA HTTP bridge endpoints."""

import json
import sys
import urllib.request
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin

BASE_URL = "http://127.0.0.1:8765"
TOKEN_HEADER = "X-NVDA-Bridge-Token"
BRIDGE_TOKEN = "nvda-bridge-secret-change-me"
HEADERS = {
    TOKEN_HEADER: BRIDGE_TOKEN,
    "Content-Type": "application/json",
}


def send_request(path, method="GET", data=None):
    url = urljoin(BASE_URL, path)
    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")

    request = urllib.request.Request(url, data=body, method=method)
    for key, value in HEADERS.items():
        request.add_header(key, value)

    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            text = response.read().decode("utf-8")
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                payload = text
            return response.status, payload
    except HTTPError as exc:
        text = exc.read().decode("utf-8", errors="replace")
        return exc.code, {"error": text}
    except URLError as exc:
        return None, {"error": str(exc)}
    except Exception as exc:
        return None, {"error": str(exc)}


def pretty_print(path, status, payload):
    print(f"Request: {path}")
    print(f"Status: {status}")
    if isinstance(payload, dict):
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(payload)
    print("""
""")


def main():
    endpoints = ["/health", "/tools"]
    results = []

    for path in endpoints:
        status, payload = send_request(path)
        pretty_print(path, status, payload)
        results.append((path, status))

    action_body = {
        "request_id": "http-test-1",
        "action": "get_current_focus",
        "params": {},
    }
    status, payload = send_request("/action", method="POST", data=action_body)
    pretty_print("/action", status, payload)
    results.append(("/action", status))

    success = all(status == 200 for _, status in results)
    print("Overall result:", "PASS" if success else "FAIL")
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
