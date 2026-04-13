#!/bin/bash
# Copy entrypoint scripts to /app/.
set -euo pipefail
APP_SRC="$(dirname "$0")/../app"
cp -r "$APP_SRC"/* /app/
