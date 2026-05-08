#!/bin/bash
# Write env vars from env.txt to /etc/environment.
set -euo pipefail
ENV_FILE="$(dirname "$0")/env.txt"
[ -f "$ENV_FILE" ] || exit 0
while IFS= read -r line; do
    [ -z "$line" ] && continue
    key="${line%%=*}"
    grep -q "^${key}=" /etc/environment && \
        sed -i "s|^${key}=.*|${line}|" /etc/environment || \
        echo "$line" >> /etc/environment
done < "$ENV_FILE"
