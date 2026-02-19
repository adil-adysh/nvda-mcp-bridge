# Build and Install NVDA MCP Add-on
# This script builds and optionally installs the add-on

param(
    [switch]$Install,
    [switch]$Clean
)

Write-Host "=====================================" -ForegroundColor Cyan
Write-Host "NVDA MCP Add-on Build Script" -ForegroundColor Cyan
Write-Host "=====================================" -ForegroundColor Cyan
Write-Host ""

# Clean build artifacts if requested
if ($Clean) {
    Write-Host "Cleaning build artifacts..." -ForegroundColor Yellow
    
    # Clean SCons build directory
    if (Test-Path ".sconsign.dblite") {
        Remove-Item ".sconsign.dblite" -Force
    }
    
    # Clean any old .nvda-addon files
    Get-ChildItem -Filter "*.nvda-addon" | Remove-Item -Force
    
    Write-Host "✓ Cleaned" -ForegroundColor Green
    Write-Host ""
}

# Build the add-on
Write-Host "Building add-on..." -ForegroundColor Cyan
scons

if ($LASTEXITCODE -ne 0) {
    Write-Host "✗ Build failed!" -ForegroundColor Red
    exit 1
}

Write-Host "✓ Build successful!" -ForegroundColor Green
Write-Host ""

# Find the generated .nvda-addon file
$addonFile = Get-ChildItem -Filter "*.nvda-addon" | Select-Object -First 1

if (-not $addonFile) {
    Write-Host "✗ No .nvda-addon file found!" -ForegroundColor Red
    exit 1
}

Write-Host "Generated: $($addonFile.Name)" -ForegroundColor Green
Write-Host ""

# Install if requested
if ($Install) {
    Write-Host "Installing add-on..." -ForegroundColor Cyan
    Write-Host "This will open NVDA's add-on installation dialog." -ForegroundColor Yellow
    Write-Host "Click 'Yes' to install, then restart NVDA." -ForegroundColor Yellow
    Write-Host ""
    
    Start-Process $addonFile.FullName
    
    Write-Host "✓ Installation initiated" -ForegroundColor Green
    Write-Host ""
    Write-Host "Remember to:" -ForegroundColor Yellow
    Write-Host "1. Accept the installation in NVDA" -ForegroundColor Yellow
    Write-Host "2. Restart NVDA" -ForegroundColor Yellow
    Write-Host "3. Check NVDA log (NVDA+F1) for 'MCP Server started'" -ForegroundColor Yellow
} else {
    Write-Host "To install manually:" -ForegroundColor Yellow
    Write-Host "  1. Double-click $($addonFile.Name)" -ForegroundColor Yellow
    Write-Host "  2. Or run: .\build.ps1 -Install" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "=====================================" -ForegroundColor Cyan
Write-Host "Build Complete!" -ForegroundColor Cyan
Write-Host "=====================================" -ForegroundColor Cyan
