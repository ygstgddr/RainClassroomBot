$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
& .\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
if ($LASTEXITCODE -ne 0) { throw 'Build dependencies failed.' }
& .\.venv\Scripts\python.exe -m pytest -q tests
if ($LASTEXITCODE -ne 0) { throw 'Tests failed; build stopped.' }
& .\.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean --windowed --onedir --name RainClassroomBot --collect-all playwright --collect-all webview --collect-all pythonnet --collect-all clr_loader --hidden-import clr --icon gui/assets/yuketang.ico --add-data 'gui/web;gui/web' --add-data 'gui/assets;gui/assets' main.py
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed.' }
New-Item -ItemType Directory -Path dist\RainClassroomBot\config -Force | Out-Null
Copy-Item -LiteralPath config\config.yaml -Destination dist\RainClassroomBot\config -Force
Copy-Item -LiteralPath README.md,ADAPTER_NOTES.md,VALIDATION.md,VERSION.txt,CHANGELOG.md,THIRD_PARTY_NOTICES.md -Destination dist\RainClassroomBot -Force
Copy-Item -LiteralPath docs -Destination dist\RainClassroomBot -Recurse -Force
& .\.venv\Scripts\python.exe scripts\collect_licenses.py dist\RainClassroomBot\third_party_licenses
if ($LASTEXITCODE -ne 0) { throw 'Third-party license collection failed.' }
Write-Output 'Build ready: dist\RainClassroomBot\RainClassroomBot.exe'
