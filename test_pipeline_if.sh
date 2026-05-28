#!/bin/bash
set -e
if [ -n "token" ]; then
    python3 -c "import sys; sys.exit(1)" 2>&1 | tee >(sleep 1)
else
    echo "No token"
fi
echo "PIPESTATUS is ${PIPESTATUS[0]}"
exit ${PIPESTATUS[0]}
