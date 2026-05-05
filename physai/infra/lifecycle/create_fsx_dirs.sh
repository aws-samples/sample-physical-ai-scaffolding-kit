#!/bin/bash
# Create necessary directories in /fsx. Controller-only (FSx is shared, but
# creating these needs to happen once and the controller is the natural place).
set -ex
. "$(dirname "${BASH_SOURCE[0]}")/_lib.sh"
require_node_type controller

for d in raw datasets checkpoints evaluations physai; do
    mkdir -p /fsx/${d}
    chmod 0777 /fsx/${d}
done
