# Publishes a new ArtSafe version. Her app picks it up automatically within a few hours.
#
#   powershell -ExecutionPolicy Bypass -File release.ps1 -Version 1.0.1 -Notes "Fixed X"
#
# Needs: GitHub CLI logged in (gh auth login), and the signing key in %USERPROFILE%\.artsafe\signing_key.pem
param(
    [Parameter(Mandatory = $true)][string]$Version,
    [string]$Notes = "",
    [switch]$NoPublish   # build + sign only, for testing
)
$ErrorActionPreference = "Stop"
$py = ".venv\Scripts\python.exe"
$gh = (Get-Command gh -ErrorAction SilentlyContinue).Source
if (-not $gh) { $gh = "C:\Program Files\GitHub CLI\gh.exe" }
if ($Version -notmatch '^\d+\.\d+\.\d+$') { throw "Version must look like 1.2.3" }
$key = Join-Path $env:USERPROFILE ".artsafe\signing_key.pem"
if (-not (Test-Path $key)) { throw "Signing key not found at $key" }

# 1. version number
Set-Content artsafe\__init__.py "__version__ = `"$Version`"" -Encoding ascii

# 2. tests must pass
& $py -m pytest -q tests
if ($LASTEXITCODE -ne 0) { throw "Tests failed; nothing was released" }

# 3. build
powershell -ExecutionPolicy Bypass -File build.ps1
if ($LASTEXITCODE -ne 0) { throw "Build failed" }

# 4. zip + sign
$zip = "dist\ArtSafe-$Version.zip"
Remove-Item $zip, "$zip.sig" -ErrorAction SilentlyContinue
Compress-Archive -Path dist\ArtSafe -DestinationPath $zip -CompressionLevel Optimal
& $py -c @"
import hashlib, sys
from pathlib import Path
from cryptography.hazmat.primitives import serialization
key = serialization.load_pem_private_key(Path(r'$key').read_bytes(), None)
data = Path(r'$zip').read_bytes()
Path(r'$zip.sig').write_bytes(key.sign(hashlib.sha256(data).digest()))
print('signed', r'$zip')
"@
if ($NoPublish) { Write-Host "Built and signed $zip (not published)"; exit 0 }

# 5. commit, tag, publish
git add -A
git commit -m "Release v$Version" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git tag "v$Version"
git push origin HEAD --tags
if (-not $Notes) { $Notes = "ArtSafe $Version" }
& $gh release create "v$Version" $zip "$zip.sig" --title "ArtSafe $Version" --notes $Notes
Write-Host "Published v$Version. Her ArtSafe will update itself within ~6 hours (or via Settings > Check for updates)."
