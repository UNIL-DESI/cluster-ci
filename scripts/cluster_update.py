#!/usr/bin/env python3
"""Cluster-CI Update Script Wrapper"""
import os
import sys

# Ensure src/ is in sys.path
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_DIR = os.path.join(BASE_DIR, "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from cluster.cluster_update import main

if __name__ == "__main__":
    main()
