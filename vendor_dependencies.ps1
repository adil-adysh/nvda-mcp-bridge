# Vendor Dependencies Helper Script
# This script helps vendor Python dependencies into the addon's lib folder
# Currently not needed as we use stdlib only, but useful for future extensions

param(
    [string]$RequirementsFile = "",
    [string[]]$Packages = @(),
    [switch]$Clean
)

$libPath = Join-Path $PSScriptRoot "addon\globalPlugins\nvdaMcp\lib"

# Clean lib directory if requested
if ($Clean) {
    Write-Host "Cleaning lib directory..." -ForegroundColor Yellow
    if (Test-Path $libPath) {
        Get-ChildItem $libPath -Exclude "README.md" | Remove-Item -Recurse -Force
        Write-Host "✓ Cleaned" -ForegroundColor Green
    }
}

# Create lib directory if it doesn't exist
if (-not (Test-Path $libPath)) {
    New-Item -ItemType Directory -Path $libPath | Out-Null
    Write-Host "Created lib directory" -ForegroundColor Green
}

# Install from requirements file
if ($RequirementsFile -and (Test-Path $RequirementsFile)) {
    Write-Host "Installing from $RequirementsFile..." -ForegroundColor Cyan
    pip install --target $libPath -r $RequirementsFile --upgrade
    if ($LASTEXITCODE -eq 0) {
        Write-Host "✓ Dependencies installed successfully" -ForegroundColor Green
    } else {
        Write-Host "✗ Failed to install dependencies" -ForegroundColor Red
        exit 1
    }
}

# Install individual packages
if ($Packages.Count -gt 0) {
    Write-Host "Installing packages: $($Packages -join ', ')..." -ForegroundColor Cyan
    pip install --target $libPath $Packages --upgrade
    if ($LASTEXITCODE -eq 0) {
        Write-Host "✓ Packages installed successfully" -ForegroundColor Green
    } else {
        Write-Host "✗ Failed to install packages" -ForegroundColor Red
        exit 1
    }
}

# Show what's installed
Write-Host "`nInstalled packages in lib/:" -ForegroundColor Cyan
Get-ChildItem $libPath -Directory | ForEach-Object {
    Write-Host "  - $($_.Name)" -ForegroundColor Gray
}

Write-Host "`nNote: This addon currently uses only Python stdlib." -ForegroundColor Yellow
Write-Host "You only need to vendor dependencies if you're extending functionality." -ForegroundColor Yellow
