@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run install.ps1 first. See README.md.
  pause
  exit /b 1
)
".venv\Scripts\pythonw.exe" main.py
