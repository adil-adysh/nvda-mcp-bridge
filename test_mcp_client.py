"""
Python test client for NVDA MCP Server
Tests all available MCP tools
"""

import requests
import json
from typing import Dict, Any

BASE_URL = "http://127.0.0.1:8765"
TOKEN = "nvda-mcp-secret-token-change-me"
HEADERS = {
    "Content-Type": "application/json",
    "X-MCP-Token": TOKEN
}


def call_tool(name: str, arguments: Dict[str, Any] = None) -> Dict[str, Any]:
    """Call an MCP tool and return the response"""
    response = requests.post(
        f"{BASE_URL}/call_tool",
        headers=HEADERS,
        json={
            "name": name,
            "arguments": arguments or {}
        }
    )
    response.raise_for_status()
    return response.json()


def print_section(title: str):
    """Print a section header"""
    print("\n" + "=" * 60)
    print(f"  {title}")
    print("=" * 60)


def print_result(result: Dict[str, Any]):
    """Pretty print a result"""
    print(json.dumps(result, indent=2, ensure_ascii=False))


def main():
    """Run all tests"""
    print("=" * 60)
    print("  NVDA MCP Server - Python Test Client")
    print("=" * 60)
    
    # Test 1: Health check
    print_section("Test 1: Health Check")
    try:
        response = requests.get(f"{BASE_URL}/health")
        response.raise_for_status()
        print("✓ Health check passed")
        print_result(response.json())
    except Exception as e:
        print(f"✗ Health check failed: {e}")
        return
    
    # Test 2: List tools
    print_section("Test 2: List Available Tools")
    try:
        response = requests.get(f"{BASE_URL}/tools", headers=HEADERS)
        response.raise_for_status()
        tools_data = response.json()
        print(f"✓ Found {len(tools_data['tools'])} tools:")
        for tool in tools_data['tools']:
            print(f"\n  - {tool['name']}")
            print(f"    {tool['description']}")
    except Exception as e:
        print(f"✗ Tool listing failed: {e}")
        return
    
    # Test 3: Speak text
    print_section("Test 3: Speak Text")
    try:
        result = call_tool("speak_text", {"text": "Testing NVDA MCP integration from Python"})
        print("✓ Speak text succeeded")
        print_result(result)
    except Exception as e:
        print(f"✗ Speak text failed: {e}")
    
    # Test 4: Get current focus
    print_section("Test 4: Get Current Focus")
    try:
        result = call_tool("get_current_focus")
        print("✓ Get current focus succeeded")
        print_result(result)
    except Exception as e:
        print(f"✗ Get current focus failed: {e}")
    
    # Test 5: Read current line
    print_section("Test 5: Read Current Line")
    try:
        result = call_tool("read_current_line")
        print("✓ Read current line succeeded")
        print_result(result)
    except Exception as e:
        print(f"✗ Read current line failed: {e}")
    
    # Test 6: Get window title
    print_section("Test 6: Get Window Title")
    try:
        result = call_tool("get_window_title")
        print("✓ Get window title succeeded")
        print_result(result)
    except Exception as e:
        print(f"✗ Get window title failed: {e}")
    
    # Test 7: Get navigator object
    print_section("Test 7: Get Navigator Object")
    try:
        result = call_tool("get_navigator_object")
        print("✓ Get navigator object succeeded")
        print_result(result)
    except Exception as e:
        print(f"✗ Get navigator object failed: {e}")
    
    # Test 8: Show message
    print_section("Test 8: Show Message")
    try:
        result = call_tool("show_message", {"message": "Hello from Python MCP client!"})
        print("✓ Show message succeeded")
        print_result(result)
    except Exception as e:
        print(f"✗ Show message failed: {e}")
    
    # Test 9: Move focus (next)
    print_section("Test 9: Move Focus (Next)")
    try:
        result = call_tool("move_focus", {"direction": "next"})
        print("✓ Move focus succeeded")
        print_result(result)
    except Exception as e:
        print(f"✗ Move focus failed: {e}")
    
    # Test 10: Authentication (should fail)
    print_section("Test 10: Test Authentication (Invalid Token)")
    try:
        bad_headers = {
            "Content-Type": "application/json",
            "X-MCP-Token": "wrong-token"
        }
        response = requests.post(
            f"{BASE_URL}/call_tool",
            headers=bad_headers,
            json={"name": "get_current_focus", "arguments": {}}
        )
        if response.status_code == 401:
            print("✓ Authentication correctly rejected invalid token")
        else:
            print(f"✗ Unexpected status code: {response.status_code}")
    except Exception as e:
        print(f"✗ Authentication test failed: {e}")
    
    print_section("Test Suite Complete!")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nTest interrupted by user")
    except Exception as e:
        print(f"\n\nFatal error: {e}")
        import traceback
        traceback.print_exc()
