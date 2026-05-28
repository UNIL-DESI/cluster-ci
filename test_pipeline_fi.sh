#!/bin/bash
set -e
if true; then
    python3 -c "import sys; sys.exit(42)" | tee /dev/null
fi
echo "PIPESTATUS is ${PIPESTATUS[0]}"
