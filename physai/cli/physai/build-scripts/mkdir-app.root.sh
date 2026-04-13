#!/bin/bash
# Create /app with world-writable permissions.
set -euo pipefail
mkdir -m 777 -p /app
