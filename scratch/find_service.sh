#!/bin/bash
source .env
sshpass -p "$HEADNODE_PASS" ssh -o StrictHostKeyChecking=no ${HEADNODE_USER}@${HEADNODE_IP} \
  "ps aux | grep headnode; echo '---'; ls -la /home/henri/cluster-ci/*.log; echo '---'; systemctl list-units --type=service | grep -i cluster 2>/dev/null; echo '---'; docker ps 2>/dev/null | grep -i cluster"
