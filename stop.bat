@echo off
REM OneChain — stop (Windows)
REM Double-click this file to stop OneChain.
cd /d "%~dp0"

echo Stopping OneChain...
docker compose down

echo.
echo OneChain has been stopped. Your data was not touched.
pause
