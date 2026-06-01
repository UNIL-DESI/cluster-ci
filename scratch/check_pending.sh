#!/bin/bash
source .env
sshpass -p "$HEADNODE_PASS" ssh -o StrictHostKeyChecking=no ${HEADNODE_USER}@${HEADNODE_IP} \
  "sqlite3 -header -column /home/henri/cluster-ci/cluster_scheduler.db \
  \"SELECT job_id, repo, branch, username, status, created_at FROM jobs WHERE status IN ('pending','running','assigned') ORDER BY created_at DESC LIMIT 10;\""
