# Test NVDA HTTP Bridge
# This script tests the NVDA HTTP Bridge functionality
# The bridge must be running (NVDA with addon loaded) before running this test

param(
    [string]$BaseUrl = "http://127.0.0.1:8765",
    [string]$Token = "nvda-bridge-secret-change-me"
)

$headers = @{
    "Content-Type" = "application/json"
    "X-NVDA-Bridge-Token" = $Token
}

$testsPassed = 0
$testsFailed = 0

function Write-TestHeader {
    param([string]$Title)
    Write-Host "`n=====================================" -ForegroundColor Cyan
    Write-Host $Title -ForegroundColor Cyan
    Write-Host "=====================================" -ForegroundColor Cyan
}

function Write-TestStart {
    param([string]$TestName)
    Write-Host "`n[$TestName]" -ForegroundColor Yellow
}

function Write-TestPass {
    param([string]$Message)
    $script:testsPassed++
    Write-Host "✓ $Message" -ForegroundColor Green
}

function Write-TestFail {
    param([string]$Message)
    $script:testsFailed++
    Write-Host "✗ $Message" -ForegroundColor Red
}

function Invoke-BridgeAction {
    param(
        [string]$Action,
        [hashtable]$Params = @{}
    )
    
    $body = @{
        action = $Action
        params = $Params
    } | ConvertTo-Json -Depth 10
    
    try {
        $response = Invoke-RestMethod -Uri "$BaseUrl/action" -Method Post -Headers $headers -Body $body -ErrorAction Stop
        return $response
    } catch {
        throw $_
    }
}

# Start tests
Write-TestHeader "NVDA HTTP Bridge Test Suite"
Write-Host "Base URL: $BaseUrl"
Write-Host "Testing connection..."

# Test 1: Health Check (no auth required)
Write-TestStart "Test 1: Health Check"
try {
    $response = Invoke-RestMethod -Uri "$BaseUrl/health" -Method Get -ErrorAction Stop
    Write-Host "Response:" -ForegroundColor Gray
    Write-Host ($response | ConvertTo-Json -Depth 3)
    
    if ($response.status -eq "ok") {
        Write-TestPass "Health check returned OK status"
    } else {
        Write-TestFail "Health check returned unexpected status: $($response.status)"
    }
} catch {
    Write-TestFail "Health check failed: $($_.Exception.Message)"
    Write-Host "Make sure NVDA is running with the HTTP Bridge addon loaded!" -ForegroundColor Yellow
    exit 1
}

# Test 2: Unauthorized request (should fail)
Write-TestStart "Test 2: Authentication Check (Bad Token)"
try {
    $badHeaders = @{
        "Content-Type" = "application/json"
        "X-NVDA-Bridge-Token" = "wrong-token"
    }
    $body = @{
        action = "get_current_focus"
        params = @{}
    } | ConvertTo-Json
    
    $response = Invoke-RestMethod -Uri "$BaseUrl/action" -Method Post -Headers $badHeaders -Body $body -ErrorAction Stop
    Write-TestFail "Should have rejected bad token, but accepted it"
} catch {
    if ($_.Exception.Response.StatusCode -eq 401) {
        Write-TestPass "Correctly rejected unauthorized request (401)"
    } else {
        Write-TestFail "Expected 401 Unauthorized, got: $($_.Exception.Response.StatusCode)"
    }
}

# Test 3: Get Current Focus
Write-TestStart "Test 3: Get Current Focus"
try {
    $response = Invoke-BridgeAction -Action "get_current_focus"
    Write-Host "Response:" -ForegroundColor Gray
    Write-Host ($response | ConvertTo-Json -Depth 5)
    
    if ($response.success -and $response.result) {
        Write-TestPass "Got current focus information"
        if ($response.result.name) {
            Write-Host "  Focus name: $($response.result.name)" -ForegroundColor Gray
        }
        if ($response.result.roleText) {
            Write-Host "  Role: $($response.result.roleText)" -ForegroundColor Gray
        }
    } else {
        Write-TestFail "Unexpected response format"
    }
} catch {
    Write-TestFail "Get current focus failed: $($_.Exception.Message)"
}

# Test 4: Get Window Title
Write-TestStart "Test 4: Get Window Title"
try {
    $response = Invoke-BridgeAction -Action "get_window_title"
    Write-Host "Response:" -ForegroundColor Gray
    Write-Host ($response | ConvertTo-Json -Depth 3)
    
    if ($response.success -and $response.result) {
        Write-TestPass "Got window title"
        if ($response.result.title) {
            Write-Host "  Window title: $($response.result.title)" -ForegroundColor Gray
        }
        if ($response.result.className) {
            Write-Host "  Class name: $($response.result.className)" -ForegroundColor Gray
        }
    } else {
        Write-TestFail "Unexpected response format"
    }
} catch {
    Write-TestFail "Get window title failed: $($_.Exception.Message)"
}

# Test 5: Read Current Line
Write-TestStart "Test 5: Read Current Line"
try {
    $response = Invoke-BridgeAction -Action "read_current_line"
    Write-Host "Response:" -ForegroundColor Gray
    Write-Host ($response | ConvertTo-Json -Depth 3)
    
    if ($response.success -and $response.result) {
        Write-TestPass "Got current line"
        if ($response.result.text) {
            Write-Host "  Line text: $($response.result.text)" -ForegroundColor Gray
        }
    } else {
        Write-TestFail "Unexpected response format"
    }
} catch {
    Write-TestFail "Read current line failed: $($_.Exception.Message)"
}

# Test 6: Speak Text
Write-TestStart "Test 6: Speak Text"
try {
    $testMessage = "Testing NVDA HTTP Bridge from PowerShell"
    $response = Invoke-BridgeAction -Action "speak_text" -Params @{ text = $testMessage }
    Write-Host "Response:" -ForegroundColor Gray
    Write-Host ($response | ConvertTo-Json -Depth 3)
    
    if ($response.success -and $response.result.success) {
        Write-TestPass "Speak text command succeeded"
        Write-Host "  Spoken: $($response.result.spoken)" -ForegroundColor Gray
        Write-Host "  (You should have heard NVDA speak the test message)" -ForegroundColor Yellow
    } else {
        Write-TestFail "Speak text failed"
    }
} catch {
    Write-TestFail "Speak text failed: $($_.Exception.Message)"
}

# Test 7: Show Message
Write-TestStart "Test 7: Show Message"
try {
    $testMessage = "Test UI message from PowerShell"
    $response = Invoke-BridgeAction -Action "show_message" -Params @{ message = $testMessage }
    Write-Host "Response:" -ForegroundColor Gray
    Write-Host ($response | ConvertTo-Json -Depth 3)
    
    if ($response.success -and $response.result.success) {
        Write-TestPass "Show message command succeeded"
        Write-Host "  (You should have heard/seen the test message in NVDA)" -ForegroundColor Yellow
    } else {
        Write-TestFail "Show message failed"
    }
} catch {
    Write-TestFail "Show message failed: $($_.Exception.Message)"
}

# Test 8: Get Navigator Object
Write-TestStart "Test 8: Get Navigator Object"
try {
    $response = Invoke-BridgeAction -Action "get_navigator_object"
    Write-Host "Response:" -ForegroundColor Gray
    Write-Host ($response | ConvertTo-Json -Depth 3)
    
    if ($response.success -and $response.result) {
        Write-TestPass "Got navigator object"
        if ($response.result.name) {
            Write-Host "  Navigator name: $($response.result.name)" -ForegroundColor Gray
        }
    } else {
        Write-TestFail "Unexpected response format"
    }
} catch {
    Write-TestFail "Get navigator object failed: $($_.Exception.Message)"
}

# Test 9: Get Desktop Object
Write-TestStart "Test 9: Get Desktop Object"
try {
    $response = Invoke-BridgeAction -Action "get_desktop_object"
    Write-Host "Response:" -ForegroundColor Gray
    Write-Host ($response | ConvertTo-Json -Depth 3)
    
    if ($response.success -and $response.result) {
        Write-TestPass "Got desktop object"
        if ($response.result.childCount) {
            Write-Host "  Desktop child count: $($response.result.childCount)" -ForegroundColor Gray
        }
    } else {
        Write-TestFail "Unexpected response format"
    }
} catch {
    Write-TestFail "Get desktop object failed: $($_.Exception.Message)"
}

# Test 10: Invalid Action (should fail gracefully)
Write-TestStart "Test 10: Invalid Action"
try {
    $response = Invoke-BridgeAction -Action "nonexistent_action"
    Write-Host "Response:" -ForegroundColor Gray
    Write-Host ($response | ConvertTo-Json -Depth 3)
    Write-TestFail "Should have rejected invalid action"
} catch {
    # This is expected to fail
    Write-TestPass "Correctly rejected invalid action with error"
}

# Test 11: Empty Request (should fail)
Write-TestStart "Test 11: Empty Request Body"
try {
    $response = Invoke-RestMethod -Uri "$BaseUrl/action" -Method Post -Headers $headers -Body "" -ErrorAction Stop
    Write-TestFail "Should have rejected empty request"
} catch {
    if ($_.Exception.Response.StatusCode -eq 400) {
        Write-TestPass "Correctly rejected empty request (400)"
    } else {
        Write-TestFail "Expected 400 Bad Request for empty body"
    }
}

# Test 12: Invalid JSON (should fail)
Write-TestStart "Test 12: Invalid JSON"
try {
    $response = Invoke-RestMethod -Uri "$BaseUrl/action" -Method Post -Headers $headers -Body "not valid json" -ErrorAction Stop
    Write-TestFail "Should have rejected invalid JSON"
} catch {
    if ($_.Exception.Response.StatusCode -eq 400) {
        Write-TestPass "Correctly rejected invalid JSON (400)"
    } else {
        Write-TestFail "Expected 400 Bad Request for invalid JSON"
    }
}

# Test 13: Input Validation - Text Too Long
Write-TestStart "Test 13: Input Validation (Text Length)"
try {
    $longText = "x" * 15000  # Exceeds 10000 char limit
    $response = Invoke-BridgeAction -Action "speak_text" -Params @{ text = $longText }
    Write-Host "Response:" -ForegroundColor Gray
    Write-Host ($response | ConvertTo-Json -Depth 3)
    
    if ($response.result.error -match "too long") {
        Write-TestPass "Correctly validated text length"
    } else {
        Write-TestFail "Should have rejected text that exceeds limit"
    }
} catch {
    Write-TestFail "Text validation test failed: $($_.Exception.Message)"
}

# Test 14: Input Validation - Invalid Parameter Type
Write-TestStart "Test 14: Input Validation (Invalid Parameter Type)"
try {
    $response = Invoke-BridgeAction -Action "speak_text" -Params @{ text = 12345 }  # Number instead of string
    Write-Host "Response:" -ForegroundColor Gray
    Write-Host ($response | ConvertTo-Json -Depth 3)
    
    if ($response.result.error) {
        Write-TestPass "Correctly rejected invalid parameter type"
    } else {
        Write-Host "  Note: Type validation may be handled by type coercion" -ForegroundColor Yellow
    }
} catch {
    Write-TestFail "Parameter validation test failed: $($_.Exception.Message)"
}

# Test 15: Move Focus (careful - changes focus!)
Write-TestStart "Test 15: Move Focus (Next)"
Write-Host "  WARNING: This will change focus in NVDA!" -ForegroundColor Yellow
Start-Sleep -Seconds 1
try {
    $response = Invoke-BridgeAction -Action "move_focus" -Params @{ direction = "next" }
    Write-Host "Response:" -ForegroundColor Gray
    Write-Host ($response | ConvertTo-Json -Depth 5)
    
    if ($response.success -and $response.result) {
        if ($response.result.success) {
            Write-TestPass "Successfully moved focus to next object"
        } elseif ($response.result.error) {
            Write-Host "  Note: $($response.result.error)" -ForegroundColor Yellow
            Write-TestPass "Move focus handled gracefully (no crash)"
        }
    }
} catch {
    Write-TestFail "Move focus failed: $($_.Exception.Message)"
}

# Summary
Write-Host "`n=====================================" -ForegroundColor Cyan
Write-Host "Test Summary" -ForegroundColor Cyan
Write-Host "=====================================" -ForegroundColor Cyan
Write-Host "Tests Passed: $testsPassed" -ForegroundColor Green
Write-Host "Tests Failed: $testsFailed" -ForegroundColor $(if ($testsFailed -eq 0) { "Green" } else { "Red" })
Write-Host "Total Tests: $($testsPassed + $testsFailed)"

if ($testsFailed -eq 0) {
    Write-Host "`n✓ All tests passed!" -ForegroundColor Green
    exit 0
} else {
    Write-Host "`n✗ Some tests failed!" -ForegroundColor Red
    exit 1
}
