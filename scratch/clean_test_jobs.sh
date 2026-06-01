#!/bin/bash
source .env
# Clean test jobs created by update_cluster.sh and show current state
sshpass -p "$HEADNODE_PASS" ssh -o StrictHostKeyChecking=no ${HEADNODE_USER}@${HEADNODE_IP} \
  "sqlite3 /home/henri/cluster-ci/cluster_scheduler.db \
  \"UPDATE jobs SET status='failed', exit_code=-15, finished_at=CURRENT_TIMESTAMP WHERE status='pending' AND repo='UNIL-DESI/cluster-ci';\" && \
  echo 'Cleaned test jobs. Current active:' && \
  sqlite3 -header -column /home/henri/cluster-ci/cluster_scheduler.db \
  \"SELECT job_id, repo, branch, username, status, created_at FROM jobs WHERE status IN ('pending','running','assigned') ORDER BY created_at DESC;\""
