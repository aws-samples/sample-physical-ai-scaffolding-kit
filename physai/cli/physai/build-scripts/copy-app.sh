#!/bin/bash
# Copy entrypoint scripts to /app/.
# Trailing "/." handles empty app/ (pure base containers) without failing.
set -euo pipefail
APP_SRC="$(dirname "$0")/../app"
cp -r "$APP_SRC"/. /app/
