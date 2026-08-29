# Builds the Windows distribution zip for LiveTranslator-kun.
# Run from bin/windows/ in a plain PowerShell prompt (not this SSH session -
# PyInstaller needs a real interactive desktop for some of its probing, and
# the app itself can only be smoke-tested from one anyway):
#
#   cd bin\windows
#   .\build_dist.ps1
#
# See ../../docs/BUILDING_WINDOWS.md for what this does and why, and for
# manual steps if you'd rather not run a script blind.

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$venv = Join-Path $root ".build-venv"
if (-not (Test-Path $venv)) {
    python -m venv $venv
}
$venvPython = Join-Path $venv "Scripts\python.exe"

& $venvPython -m pip install --upgrade pip
& $venvPython -m pip install -r requirements.txt pyinstaller

Remove-Item -Recurse -Force -ErrorAction SilentlyContinue (Join-Path $root "licenses")
& $venvPython collect_licenses.py

Remove-Item -Recurse -Force -ErrorAction SilentlyContinue (Join-Path $root "build")
Remove-Item -Recurse -Force -ErrorAction SilentlyContinue (Join-Path $root "dist")
& $venvPython -m PyInstaller livetranslate.spec --noconfirm

$distDir = Join-Path $root "dist\LiveTranslator-kun"
Copy-Item (Join-Path $root "..\..\LICENSE") $distDir
Copy-Item (Join-Path $root "..\..\THIRD_PARTY_LICENSES.md") $distDir
Copy-Item -Recurse (Join-Path $root "licenses") (Join-Path $distDir "licenses")

$version = (Get-Content (Join-Path $root "..\..\VERSION") -ErrorAction SilentlyContinue)
if (-not $version) { $version = "dev" }
$zipPath = Join-Path $root "dist\LiveTranslator-kun-windows-$version.zip"
Remove-Item -Force -ErrorAction SilentlyContinue $zipPath
Compress-Archive -Path $distDir -DestinationPath $zipPath

Write-Host "Built $zipPath"
