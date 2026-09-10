# Builds PrintAgentSetup.exe from source. Run this from inside the
# print-agent folder in PowerShell whenever the agent's code changes:
#
#   .\build.ps1
#
# Output: dist\PrintAgentSetup.exe - this single file is everything a
# customer needs; hand it out on its own (no installer, no "also install
# Python" step for them).

$ErrorActionPreference = "Stop"

$sumatraPath = "vendor\SumatraPDF.exe"
if (-not (Test-Path $sumatraPath)) {
    Write-Host "vendor\SumatraPDF.exe is missing - downloading the official portable build..."
    $zipPath = "vendor\_sumatra.zip"
    New-Item -ItemType Directory -Force -Path "vendor" | Out-Null
    Invoke-WebRequest -Uri "https://www.sumatrapdfreader.org/dl/rel/3.6.1/SumatraPDF-3.6.1-64.zip" -OutFile $zipPath
    Expand-Archive -Path $zipPath -DestinationPath "vendor" -Force
    Get-ChildItem "vendor\SumatraPDF-*-64.exe" | Rename-Item -NewName "SumatraPDF.exe"
    Remove-Item $zipPath
}

pip install -r requirements.txt

pyinstaller --onefile --windowed --name PrintAgentSetup `
    --add-binary "$sumatraPath;vendor" `
    main.py

Write-Host ""
Write-Host "Done. The finished program is at dist\PrintAgentSetup.exe"
