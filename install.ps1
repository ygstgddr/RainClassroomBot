$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
python -m venv .venv
if ($LASTEXITCODE -ne 0) { throw 'Python 3.11+ is required.' }
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
& .\.venv\Scripts\python.exe main.py --self-check
if ($LASTEXITCODE -ne 0) { throw 'Configuration or dependency check failed.' }
Write-Output 'Installation finished. Run start.cmd.'
