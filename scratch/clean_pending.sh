#!/bin/bash
source .env
# Cancel the 2 oldest pending jobs (keep only the latest pending + the running one)
sshpass -p "$HEADNODE_PASS" ssh -o StrictHostKeyChecking=no ${HEADNODE_USER}@${HEADNODE_IP} \
  "sqlite3 /home/henri/cluster-ci/cluster_scheduler.db \
  \"UPDATE jobs SET status='failed', exit_code=-15, finished_at=CURRENT_TIMESTAMP WHERE job_id IN ('63b256a1-0de1-4916-85d2-203c5d483017', '27269ee1-74c6-431e-87d9-72e6eb734c2c', '058a0623-b47a-46a9-8d91-bcd8e5c6959b');\" && \
  echo 'Done. Remaining active jobs:' && \
  sqlite3 -header -column /home/henri/cluster-ci/cluster_scheduler.db \
  \"SELECT job_id, status, created_at FROM jobs WHERE status IN ('pending','running','assigned') ORDER BY created_at DESC;\""
