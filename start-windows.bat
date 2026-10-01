@echo off
rem Dubbelklik om ViralClip AI te starten (Windows).
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start.ps1"
echo.
pause
