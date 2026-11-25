#!/bin/bash
# OneChain — stop (Mac)
# Double-click this file to stop OneChain.
cd "$(dirname "$0")" || exit 1

echo "Stopping OneChain..."
docker compose down

echo ""
echo "OneChain has been stopped. Your data was not touched."
read -n 1 -s -r -p "Press any key to close this window..."
