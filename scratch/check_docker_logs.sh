#!/bin/bash
source .env
sshpass -p "$HEADNODE_PASS" ssh -o StrictHostKeyChecking=no ${HEADNODE_USER}@${HEADNODE_IP} \
  "docker logs cluster-ci-headnode 2>&1 | grep -i 'AUTO-CANCEL\|cancel_job_cleanly\|submit_job' | tail -30"
