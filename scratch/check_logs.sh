#!/bin/bash
source .env
sshpass -p "$HEADNODE_PASS" ssh -o StrictHostKeyChecking=no ${HEADNODE_USER}@${HEADNODE_IP} \
  "journalctl -u cluster-ci --no-pager -n 50 2>/dev/null || tail -50 /home/henri/cluster-ci/ci.log 2>/dev/null || echo 'No logs found'"
