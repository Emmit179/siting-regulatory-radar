$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function Find-Python {
    $candidates = @(
        @{ Command = "py"; Args = @("-3.13") },
        @{ Command = "py"; Args = @("-3.12") },
        @{ Command = "py"; Args = @("-3.11") },
        @{ Command = "python"; Args = @() }
    )
    foreach ($candidate in $candidates) {
        try {
            $version = & $candidate.Command @($candidate.Args) -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
            if ($LASTEXITCODE -eq 0 -and [version]$version -ge [version]"3.11" -and [version]$version -lt [version]"3.14") {
                return $candidate
            }
        } catch { }
    }
    throw "Python 3.11, 3.12, or 3.13 was not found. Install it from https://www.python.org/downloads/windows/ and check 'Add python.exe to PATH'."
}

$Python = Find-Python
Write-Host "Using Python $(& $Python.Command @($Python.Args) --version)" -ForegroundColor Green

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Host "Creating isolated Python environment..."
    & $Python.Command @($Python.Args) -m venv .venv
}

$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"
& $VenvPython -m pip install --upgrade pip
& $VenvPython -m pip install -e ".[dev]"

if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "Created .env from .env.example" -ForegroundColor Green
}

Write-Host "Installing the headless Chromium browser used for JavaScript meeting portals..."
& $VenvPython -m playwright install chromium

$Tesseract = Get-Command tesseract -ErrorAction SilentlyContinue
if (-not $Tesseract) {
    $CommonTesseract = "C:\Program Files\Tesseract-OCR\tesseract.exe"
    $LocalTesseract = Join-Path $env:LOCALAPPDATA "Programs\Tesseract-OCR\tesseract.exe"
    if (-not (Test-Path $CommonTesseract) -and -not (Test-Path $LocalTesseract)) {
        $Winget = Get-Command winget -ErrorAction SilentlyContinue
        if ($Winget) {
            Write-Host "Installing Tesseract OCR for scanned meeting packets..."
            & winget install --id tesseract-ocr.tesseract -e --silent --accept-package-agreements --accept-source-agreements
            if ($LASTEXITCODE -ne 0) {
                Write-Warning "The current Tesseract package did not install; trying the legacy Windows package."
                & winget install --id UB-Mannheim.TesseractOCR -e --silent --accept-package-agreements --accept-source-agreements
            }
            if ($LASTEXITCODE -ne 0) {
                Write-Warning "Tesseract installation was skipped. Text PDFs still work; GitHub Actions installs OCR automatically."
            }
        } else {
            Write-Warning "Tesseract was not found. Text PDFs still work; install Tesseract later for scanned PDFs."
        }
    }
}

& $VenvPython -m countywatch bootstrap
& $VenvPython -m countywatch doctor

Write-Host ""
Write-Host "Next:" -ForegroundColor Cyan
Write-Host "  1. Open .env in Notepad and paste at least one free API key."
Write-Host "  2. Double-click update-now.bat."
Write-Host "  3. Double-click start-dashboard.bat."
