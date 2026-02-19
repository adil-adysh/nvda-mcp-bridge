# Test MCP Server
# This script tests the NVDA MCP Server functionality
# Run this after installing the add-on in NVDA

$baseUrl = "http://127.0.0.1:8765"
$token = "nvda-mcp-secret-token-change-me"

$headers = @{
    "Content-Type" = "application/json"
    "X-MCP-Token" = $token
}

Write-Host "=====================================" -ForegroundColor Cyan
Write-Host "NVDA MCP Server Test Suite" -ForegroundColor Cyan
Write-Host "=====================================" -ForegroundColor Cyan
Write-Host ""

# Test 1: Health check (no auth required)
Write-Host "Test 1: Health Check" -ForegroundColor Yellow
try {
    $response = Invoke-RestMethod -Uri "$baseUrl/health" -Method Get
    Write-Host "✓ Health check passed" -ForegroundColor Green
    Write-Host ($response | ConvertTo-Json)
} catch {
    Write-Host "✗ Health check failed: $_" -ForegroundColor Red
    exit 1
}
Write-Host ""

# Test 2: List tools
Write-Host "Test 2: List Available Tools" -ForegroundColor Yellow
try {
    $response = Invoke-RestMethod -Uri "$baseUrl/tools" -Method Get -Headers $headers
    Write-Host "✓ Tool listing succeeded" -ForegroundColor Green
    Write-Host "Available tools:" -ForegroundColor Cyan
    foreach ($tool in $response.tools) {
        Write-Host "  - $($tool.name): $($tool.description)" -ForegroundColor Gray
    }
} catch {
    Write-Host "✗ Tool listing failed: $_" -ForegroundColor Red
    exit 1
}
Write-Host ""

# Test 3: Speak text
Write-Host "Test 3: Speak Text" -ForegroundColor Yellow
try {
    $body = @{
        name = "speak_text"
        arguments = @{
            text = "Testing NVDA MCP Server integration"
        }
    } | ConvertTo-Json
    
    $response = Invoke-RestMethod -Uri "$baseUrl/call_tool" -Method Post -Headers $headers -Body $body
    Write-Host "✓ Speak text succeeded" -ForegroundColor Green
    Write-Host ($response | ConvertTo-Json)
} catch {
    Write-Host "✗ Speak text failed: $_" -ForegroundColor Red
}
Write-Host ""

# Test 4: Get current focus
Write-Host "Test 4: Get Current Focus" -ForegroundColor Yellow
try {
    $body = @{
        name = "get_current_focus"
        arguments = @{}
    } | ConvertTo-Json
    
    $response = Invoke-RestMethod -Uri "$baseUrl/call_tool" -Method Post -Headers $headers -Body $body
    Write-Host "✓ Get current focus succeeded" -ForegroundColor Green
    Write-Host ($response | ConvertTo-Json -Depth 5)
} catch {
    Write-Host "✗ Get current focus failed: $_" -ForegroundColor Red
}
Write-Host ""

# Test 5: Get window title
Write-Host "Test 5: Get Window Title" -ForegroundColor Yellow
try {
    $body = @{
        name = "get_window_title"
        arguments = @{}
    } | ConvertTo-Json
    
    $response = Invoke-RestMethod -Uri "$baseUrl/call_tool" -Method Post -Headers $headers -Body $body
    Write-Host "✓ Get window title succeeded" -ForegroundColor Green
    Write-Host ($response | ConvertTo-Json)
} catch {
    Write-Host "✗ Get window title failed: $_" -ForegroundColor Red
}
Write-Host ""

# Test 6: Read current line
Write-Host "Test 6: Read Current Line" -ForegroundColor Yellow
try {
    $body = @{
        name = "read_current_line"
        arguments = @{}
    } | ConvertTo-Json
    
    $response = Invoke-RestMethod -Uri "$baseUrl/call_tool" -Method Post -Headers $headers -Body $body
    Write-Host "✓ Read current line succeeded" -ForegroundColor Green
    Write-Host ($response | ConvertTo-Json)
} catch {
    Write-Host "✗ Read current line failed: $_" -ForegroundColor Red
}
Write-Host ""

# Test 7: Show message
Write-Host "Test 7: Show Message" -ForegroundColor Yellow
try {
    $body = @{
        name = "show_message"
        arguments = @{
            message = "Test message from MCP Server"
        }
    } | ConvertTo-Json
    
    $response = Invoke-RestMethod -Uri "$baseUrl/call_tool" -Method Post -Headers $headers -Body $body
    Write-Host "✓ Show message succeeded" -ForegroundColor Green
    Write-Host ($response | ConvertTo-Json)
} catch {
    Write-Host "✗ Show message failed: $_" -ForegroundColor Red
}
Write-Host ""

# Test 8: Test authentication (should fail)
Write-Host "Test 8: Test Authentication (Invalid Token)" -ForegroundColor Yellow
try {
    $badHeaders = @{
        "Content-Type" = "application/json"
        "X-MCP-Token" = "wrong-token"
    }
    $body = @{
        name = "get_current_focus"
        arguments = @{}
    } | ConvertTo-Json
    
    $response = Invoke-RestMethod -Uri "$baseUrl/call_tool" -Method Post -Headers $badHeaders -Body $body
    Write-Host "✗ Authentication test failed: Should have been rejected" -ForegroundColor Red
} catch {
    if ($_.Exception.Response.StatusCode -eq 401) {
        Write-Host "✓ Authentication correctly rejected invalid token" -ForegroundColor Green
    } else {
        Write-Host "✗ Unexpected error: $_" -ForegroundColor Red
    }
}
Write-Host ""

Write-Host "=====================================" -ForegroundColor Cyan
Write-Host "Test Suite Complete!" -ForegroundColor Cyan
Write-Host "=====================================" -ForegroundColor Cyan
