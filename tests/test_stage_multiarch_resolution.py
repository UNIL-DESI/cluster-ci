import os
import sys
import json
import uuid
import pytest

# Ensure scheduler directory is on sys.path
sched_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "scheduler")
if sched_dir not in sys.path:
    sys.path.insert(0, sched_dir)

import persistence
import scheduler_loop
from persistence import get_db_conn

@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    db_file = str(tmp_path / f"test_cluster_{uuid.uuid4().hex[:8]}.db")
    monkeypatch.setenv("CLUSTER_DB_PATH", db_file)
    persistence.DB_PATH = db_file
    persistence.init_db()
    yield db_file

def test_jit_image_resolution_arm64_vs_amd64():
    """
    H3: Teste la résolution JIT de l'image (image_amd64 vs image_arm64) selon l'architecture du worker.
    """
    jid = "job-multiarch-01"
    img_amd = "custom/runtime:x86"
    img_arm = "custom/runtime:arm64"

    w_x86 = {
        "worker_id": "worker-x86",
        "hostname": "worker-x86",
        "status": "online",
        "cpus": 8,
        "total_ram_gb": 32.0,
        "available_storage_gb": 100.0,
        "gpu_count": 0,
        "vram_per_gpu": "[]",
        "service_url": "http://10.0.0.1:8080",
        "arch": "x86_64",
    }
    w_arm = {
        "worker_id": "worker-arm",
        "hostname": "worker-arm",
        "status": "online",
        "cpus": 8,
        "total_ram_gb": 32.0,
        "available_storage_gb": 100.0,
        "gpu_count": 0,
        "vram_per_gpu": "[]",
        "service_url": "http://10.0.0.2:8080",
        "arch": "aarch64",
    }

    with get_db_conn() as conn:
        cursor = conn.cursor()
        for w in (w_x86, w_arm):
            cursor.execute("""
                INSERT INTO workers (worker_id, hostname, status, cpus, total_ram_gb, available_storage_gb, gpu_count, vram_per_gpu, service_url, arch, last_seen)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """, (w["worker_id"], w["hostname"], w["status"], w["cpus"], w["total_ram_gb"], w["available_storage_gb"], w["gpu_count"], w["vram_per_gpu"], w["service_url"], w["arch"]))

        cursor.execute("""
            INSERT INTO jobs (job_id, status, parallel_mode, home_worker, active_workers, created_at, username)
            VALUES (?, 'running', 1, 'worker-x86', ?, CURRENT_TIMESTAMP, 'alice')
        """, (jid, json.dumps(["worker-x86", "worker-arm"])))

        # Deux nœuds multi-arch prêts
        res_multi = {"image_amd64": img_amd, "image_arm64": img_arm, "cpus": 2, "ram_gb": 4.0}
        cursor.execute("""
            INSERT INTO job_nodes (job_id, node_name, status, resources)
            VALUES (?, 'node_x86_turn', 'ready', ?),
                   (?, 'node_arm_turn', 'ready', ?)
        """, (jid, json.dumps(res_multi), jid, json.dumps(res_multi)))
        conn.commit()

    # 1. Demande de next_node par worker-x86
    req_x86 = {
        "job_id": jid,
        "worker_id": "worker-x86",
        "runner_id": "runner-1",
    }
    resp_x86 = scheduler_loop.handle_next_node(req_x86)
    assert resp_x86.get("action") in ("run", "switch_image")
    assert resp_x86.get("image") == img_amd, f"Expected {img_amd}, got {resp_x86.get('image')}"

    # 2. Demande de next_node par worker-arm
    req_arm = {
        "job_id": jid,
        "worker_id": "worker-arm",
        "runner_id": "runner-2",
    }
    resp_arm = scheduler_loop.handle_next_node(req_arm)
    assert resp_arm.get("action") in ("run", "switch_image")
    assert resp_arm.get("image") == img_arm, f"Expected {img_arm}, got {resp_arm.get('image')}"


def test_missing_arch_machine_keeps_node_pending_without_failure():
    """
    H3: Teste que si un nœud requiert une architecture (ex: arm64) mais qu'aucune
    machine de cette architecture n'est actuellement en ligne, le nœud ne fait pas échouer le job.
    """
    w_x86 = {
        "worker_id": "worker-x86",
        "hostname": "worker-x86",
        "status": "online",
        "cpus": 8,
        "total_ram_gb": 32.0,
        "available_storage_gb": 100.0,
        "gpu_count": 0,
        "vram_per_gpu": "[]",
        "service_url": "http://10.0.0.1:8080",
        "arch": "x86_64",
    }
    workers = [w_x86]

    res_arm_exclusive = {"image_arm64": "runtime:arm-only", "cpus": 2, "ram_gb": 4.0}
    is_impossible, err_msg = scheduler_loop.check_resource_impossibility(res_arm_exclusive, workers, item_name="stage_arm")
    assert is_impossible is False, "A node with image_arm64 when only x86 workers are online must NOT be marked impossible"
    assert err_msg == ""
