@echo off
rem Dubbelklik om ViralClip AI te stoppen (Windows). Je clips en instellingen blijven bewaard.
cd /d "%~dp0"
docker compose stop
echo.
echo ViralClip AI is gestopt. Opnieuw starten: dubbelklik start-windows.bat
pause
