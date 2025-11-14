#!/bin/bash
set -e

# Find and set library path
LIBSTDCPP_PATH=$(find /nix/store -name 'libstdc++.so.6' 2>/dev/null | head -n 1)
if [ -n "$LIBSTDCPP_PATH" ]; then
    export LD_LIBRARY_PATH=$(dirname "$LIBSTDCPP_PATH"):$LD_LIBRARY_PATH
    echo "Set LD_LIBRARY_PATH to include: $(dirname "$LIBSTDCPP_PATH")"
fi

# Activate virtual environment and start the server
source /opt/venv/bin/activate
cd app
exec python serve.py --port $PORT --address 0.0.0.0
