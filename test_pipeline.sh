#!/bin/bash
set -e
python3 -c "import sys; print('Failing fast...'); sys.exit(1)" 2>&1 | tee >(sleep 2; echo '[INFO] Waiting for receiver...')
echo "Exiting with ${PIPESTATUS[0]}"
exit ${PIPESTATUS[0]}
