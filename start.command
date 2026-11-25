#!/bin/bash
# OneChain — start (Mac)
# Double-click this file to build (first run only) and start OneChain.
cd "$(dirname "$0")" || exit 1

if ! command -v docker >/dev/null 2>&1; then
    echo "Docker Desktop doesn't seem to be installed. Please install Docker Desktop first — see README.md."
    read -n 1 -s -r -p "Press any key to close this window..."
    exit 1
fi

if ! docker info >/dev/null 2>&1; then
    echo "Docker Desktop doesn't seem to be running. Please open Docker Desktop, wait until the whale icon in the menu bar stops animating, then run this again."
    read -n 1 -s -r -p "Press any key to close this window..."
    exit 1
fi

# The settings file. The app needs the Dhan login to fetch data, and refuses to
# start without a username and password for the dashboard.
missing=""
if [ ! -f .env ]; then
    missing="CLIENT_ID ACCESS_TOKEN DASHBOARD_USER DASHBOARD_PASSWORD"
else
    for key in CLIENT_ID ACCESS_TOKEN DASHBOARD_USER DASHBOARD_PASSWORD; do
        if ! grep -Eq "^[[:space:]]*${key}[[:space:]]*=[[:space:]]*[^[:space:]#]" .env; then
            missing="$missing $key"
        fi
    done
fi
if [ -n "$missing" ]; then
    echo "The settings file .env is missing or incomplete."
    echo ""
    echo "Copy .env.example to a file named .env in this folder and fill in:"
    for key in $missing; do
        echo "  $key"
    done
    echo ""
    echo "See README.md, 'Setup'."
    read -n 1 -s -r -p "Press any key to close this window..."
    exit 1
fi

echo "Starting OneChain (this may take a few minutes the first time)..."
if ! docker compose up -d --build; then
    echo ""
    echo "OneChain failed to start. See the error above for details."
    echo ""
    read -n 1 -s -r -p "Press any key to close this window..."
    exit 1
fi

sleep 2
open "http://localhost:8053"

echo ""
echo "OneChain should now be running, and your browser should have opened to it. If it didn't, go to:"
echo "  http://localhost:8053"
echo ""
read -n 1 -s -r -p "Press any key to close this window..."
