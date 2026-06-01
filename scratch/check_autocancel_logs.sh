#!/bin/bash
source .env
sshpass -p "$HEADNODE_PASS" ssh -o StrictHostKeyChecking=no ${HEADNODE_USER}@${HEADNODE_IP} \
  "grep -i 'AUTO-CANCEL' /home/henri/cluster-ci/ci.log | tail -20"
