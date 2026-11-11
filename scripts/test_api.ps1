#!/usr/bin/env pwsh
<#
.SYNOPSIS
    Test script for NVDA HTTP Bridge API endpoints
.DESCRIPTION
    Tests the HTTP endpoints of the NVDA MCP Bridge
#>

$baseUrl = "http://127.0.0.1:8765"

# Token discovery mirrors the add-on: env var, then the per-installation file.
$token = $env:NVDA_BRIDGE_TOKEN
if (-not $token) {
    $tokenFile = Join-Path $env:APPDATA "nvda\nvda-mcp-bridge\token.key"
    if (Test-Path $tokenFile) {
        $token = (Get-Content $tokenFile -Raw).Trim()
    }
}
if (-not $token) {
    throw "No bridge token found. Set NVDA_BRIDGE_TOKEN or run the add-on so it writes token.key."
}

$headers = @{
    "X-NVDA-Bridge-Token" = $token
    "Content-Type" = "application/json"
}

Write-Host "Testing NVDA HTTP Bridge API"
Write-Host "=============================" -ForegroundColor Cyan
Write-Host ""

# Test 1: Health Check
Write-Host "[1/5] GET /health" -ForegroundColor Yellow
try {
    $health = Invoke-RestMethod -Uri "$baseUrl/health" -Method Get
    Write-Host "Status: $($health.status)" -ForegroundColor Green
    Write-Host "Protocol: $($health.protocol) v$($health.protocol_version)" -ForegroundColor Gray
    Write-Host ""
}
catch {
    Write-Host "Error: $_" -ForegroundColor Red
    Write-Host ""
    exit 1
}

# Test 2: List Tools
Write-Host "[2/5] GET /tools" -ForegroundColor Yellow
try {
    $tools = Invoke-RestMethod -Uri "$baseUrl/tools" -Method Get -Headers $headers
    Write-Host "Retrieved $($tools.count) tools" -ForegroundColor Green
    $tools.tools | Select-Object -First 5 | ForEach-Object {
        Write-Host "  - $($_.name)" -ForegroundColor Gray
    }
    Write-Host ""
}
catch {
    Write-Host "Error: $_" -ForegroundColor Red
    Write-Host ""
}

# Test 3: Get Current Focus
Write-Host "[3/5] POST /action (get_current_focus)" -ForegroundColor Yellow
try {
    $body = @{
        request_id = [guid]::NewGuid().ToString()
        action = "get_current_focus"
        params = @{}
    } | ConvertTo-Json

    $response = Invoke-RestMethod -Uri "$baseUrl/action" -Method Post -Headers $headers -Body $body
    Write-Host "Execution time: $($response.duration_ms) ms" -ForegroundColor Green
    Write-Host ""
}
catch {
    Write-Host "Error: $_" -ForegroundColor Red
    Write-Host ""
}

# Test 4: Speak Text
Write-Host "[4/5] POST /action (speak_text)" -ForegroundColor Yellow
try {
    $body = @{
        request_id = [guid]::NewGuid().ToString()
        action = "speak_text"
        params = @{ text = "Test speech output" }
    } | ConvertTo-Json

    $response = Invoke-RestMethod -Uri "$baseUrl/action" -Method Post -Headers $headers -Body $body
    Write-Host "Execution time: $($response.duration_ms) ms" -ForegroundColor Green
    Write-Host ""
}
catch {
    Write-Host "Error: $_" -ForegroundColor Red
    Write-Host ""
}

# Test 5: Health Snapshot
Write-Host "[5/5] POST /action (health_snapshot)" -ForegroundColor Yellow
try {
    $body = @{
        request_id = [guid]::NewGuid().ToString()
        action = "health_snapshot"
        params = @{
            include_logs = $false
            include_tree = $false
            log_lines = 50
            tree_depth = 2
        }
    } | ConvertTo-Json

    $response = Invoke-RestMethod -Uri "$baseUrl/action" -Method Post -Headers $headers -Body $body
    Write-Host "Execution time: $($response.duration_ms) ms" -ForegroundColor Green
    Write-Host ""
}
catch {
    Write-Host "Error: $_" -ForegroundColor Red
    Write-Host ""
}

Write-Host "Testing complete" -ForegroundColor Cyan
