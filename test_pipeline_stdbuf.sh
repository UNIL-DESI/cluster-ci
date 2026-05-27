#!/bin/bash
set -e
python3 -c "import sys; print('Failing fast...'); sys.exit(1)" 2>&1 | stdbuf -oL -eL tee >(curl -s -X POST -H "Content-Type: text/plain" -T - -N "https://ppng.io/test-xyz-abc" || true)
echo "Exiting with ${PIPESTATUS[0]}"
exit ${PIPESTATUS[0]}
