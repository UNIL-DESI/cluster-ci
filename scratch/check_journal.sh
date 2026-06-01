#!/bin/bash
source .env
sshpass -p "$HEADNODE_PASS" ssh -o StrictHostKeyChecking=no ${HEADNODE_USER}@${HEADNODE_IP} \
  "journalctl -u cluster-scheduler --no-pager --since '2026-05-29 14:00' 2>&1 | grep -i 'AUTO-CANCEL\|cancel_job\|submit' | tail -40"
