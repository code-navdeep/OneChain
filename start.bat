@echo off
REM OneChain — start (Windows)
REM Double-click this file to build (first run only) and start OneChain.
cd /d "%~dp0"

where docker >nul 2>nul
if errorlevel 1 (
    echo Docker Desktop doesn't seem to be installed. Please install Docker Desktop first - see README.md.
    pause
    exit /b 1
)

docker info >nul 2>nul
if errorlevel 1 (
    echo Docker Desktop doesn't seem to be running. Please open Docker Desktop, wait until it says "Docker Desktop is running", then run this again.
    pause
    exit /b 1
)

REM The settings file. The app needs the Dhan login to fetch data, and refuses to
REM start without a username and password for the dashboard.
set ENV_OK=1
if not exist ".env" set ENV_OK=0
if "%ENV_OK%"=="1" (
    for %%K in (CLIENT_ID ACCESS_TOKEN DASHBOARD_USER DASHBOARD_PASSWORD) do (
        findstr /r /c:"^ *%%K *= *[^ #]" ".env" >nul 2>&1 || set ENV_OK=0
    )
)
if "%ENV_OK%"=="0" (
    echo The settings file .env is missing or incomplete.
    echo.
    echo Copy .env.example to a file named .env in this folder and fill in
    echo CLIENT_ID, ACCESS_TOKEN, DASHBOARD_USER and DASHBOARD_PASSWORD.
    echo See README.md, "Setup".
    pause
    exit /b 1
)

echo Starting OneChain (this may take a few minutes the first time)...
docker compose up -d --build
if errorlevel 1 (
    echo.
    echo OneChain failed to start. See the error above for details.
    echo.
    pause
    exit /b 1
)

timeout /t 2 /nobreak >nul
start "" "http://localhost:8053"

echo.
echo OneChain should now be running, and your browser should have opened to it. If it didn't, go to:
echo   http://localhost:8053
echo.
pause
