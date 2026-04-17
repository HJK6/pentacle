#!/bin/bash
# Deploy — kill running app, rebuild, relaunch
set -e

cd "$(dirname "$0")"

APP_NAME=$(node -e "console.log(require('./pentacle.config.js').appName)")

echo "Killing $APP_NAME..."
killall "$APP_NAME" 2>/dev/null && sleep 1 || echo "Not running"

echo "Building..."
npm run build:mac

echo "Launching..."
open "/Applications/$APP_NAME.app"

echo "Done."
