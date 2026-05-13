#!/bin/bash
# Create necessary directories in /fsx
set -ex

for d in raw datasets checkpoints evaluations physai; do
    mkdir -p /fsx/${d}
    chmod 0777 /fsx/${d}
done
