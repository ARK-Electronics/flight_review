#!/bin/bash
set -e

# Debug: show what's available
echo "=== Searching for libstdc++.so.6 ==="
find /nix/store -name 'libstdc++.so.6' 2>/dev/null | head -n 5 || echo "Not found with find"

# Try to locate the library using ldconfig or direct search
LIBSTDCPP_PATH=$(find /nix/store -name 'libstdc++.so.6' 2>/dev/null | head -n 1)
if [ -z "$LIBSTDCPP_PATH" ]; then
    echo "Trying alternative search method..."
    # Look for gcc lib directory
    LIBSTDCPP_PATH=$(find /nix/store -type d -name "lib" 2>/dev/null | grep gcc | head -n 1)/libstdc++.so.6
fi

if [ -n "$LIBSTDCPP_PATH" ] && [ -f "$LIBSTDCPP_PATH" ]; then
    export LD_LIBRARY_PATH=$(dirname "$LIBSTDCPP_PATH"):$LD_LIBRARY_PATH
    echo "Set LD_LIBRARY_PATH to include: $(dirname "$LIBSTDCPP_PATH")"
else
    echo "Warning: Could not find libstdc++.so.6"
fi

echo "Current LD_LIBRARY_PATH: $LD_LIBRARY_PATH"

# Activate virtual environment and start the server
source /opt/venv/bin/activate
cd app
exec python serve.py --port $PORT --address 0.0.0.0
