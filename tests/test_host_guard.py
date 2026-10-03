"""Unit tests for Host Guard module (Cluster-CI v3 W11)."""

import json
import pytest
from src.runner.host_guard import (
    DEFAULT_HEADNODE_CPU_RESERVE,
    DEFAULT_HEADNODE_DISK_RESERVE_GB,
    DEFAULT_HEADNODE_RAM_RESERVE_GB,
    docker_resource_args,
    docker_resource_args_string,
    format_memory_value,
    get_headnode_safe_capacities,
    is_headnode_host,
    is_unified_memory_host,
    placement_priority,
)


def test_is_headnode_detection():
    # Via flag explicite
    assert is_headnode_host({"is_headnode": True}) is True
    assert is_headnode_host({"is_headnode": False}) is False

    # Via role
    assert is_headnode_host({"role": "headnode"}) is True
    assert is_headnode_host({"role": "HEADNODE"}) is True
    assert is_headnode_host({"role": "worker"}) is False

    # Via hostname isipol09
    assert is_headnode_host({"hostname": "isipol09"}) is True
    assert is_headnode_host({"hostname": "ISIPOL09"}) is True
    assert is_headnode_host({"hostname": "HEC45801"}) is False


def test_is_unified_memory_detection():
    # Via flag explicite bool/int
    assert is_unified_memory_host({"unified_memory": True}) is True
    assert is_unified_memory_host({"unified_memory": 1}) is True
    assert is_unified_memory_host({"unified_memory": False}) is False
    assert is_unified_memory_host({"unified_memory": 0}) is False

    # Via GPU name
    assert is_unified_memory_host({"gpu_name": "NVIDIA GB10"}) is True
    assert is_unified_memory_host({"gpu_name": "2x NVIDIA GeForce RTX 3090"}) is False


def test_placement_priority(monkeypatch):
    from src.config.defaults import DEFAULT_PLACEMENT_PRIORITY, HEADNODE_PLACEMENT_PRIORITY

    headnode_host = {
        "hostname": "isipol09",
        "role": "headnode",
        "total_ram_gb": 125.0,
        "cpus": 24,
    }
    gb10_host = {
        "hostname": "HEC45801",
        "role": "worker",
        "unified_memory": True,
        "gpu_name": "NVIDIA GB10",
        "total_ram_gb": 120.0,
        "cpus": 72,
    }
    discrete_worker = {
        "hostname": "worker-gpu-01",
        "role": "worker",
        "unified_memory": False,
        "gpu_name": "RTX 4090",
        "total_ram_gb": 64.0,
        "cpus": 16,
    }

    # 1. Par défaut : toutes les machines non-headnode ont la même valeur (50), headnode 0
    assert placement_priority(headnode_host) == HEADNODE_PLACEMENT_PRIORITY  # 0
    assert placement_priority(gb10_host) == DEFAULT_PLACEMENT_PRIORITY        # 50
    assert placement_priority(discrete_worker) == DEFAULT_PLACEMENT_PRIORITY  # 50

    # 2. Surcharge explicite dans le dictionnaire du worker (ex: Henri favorise une machine spécifique)
    gb10_favored = dict(gb10_host, placement_priority=90)
    assert placement_priority(gb10_favored) == 90

    # 3. Surcharge via variable d'environnement CLUSTER_CI_PLACEMENT_PRIORITY
    monkeypatch.setenv("CLUSTER_CI_PLACEMENT_PRIORITY", "80")
    assert placement_priority(discrete_worker) == 80
    monkeypatch.delenv("CLUSTER_CI_PLACEMENT_PRIORITY", raising=False)

    # 4. Tri : le headnode reste STRICTEMENT le dernier recours
    workers = [headnode_host, discrete_worker, gb10_favored]
    sorted_workers = sorted(workers, key=placement_priority, reverse=True)
    assert sorted_workers[0]["hostname"] == "HEC45801"      # 90 (favorisé)
    assert sorted_workers[1]["hostname"] == "worker-gpu-01" # 50 (défaut)
    assert sorted_workers[2]["hostname"] == "isipol09"      # 0 (dernier recours)


def test_format_memory_value():
    assert format_memory_value(2.0) == "2g"
    assert format_memory_value(16.0) == "16g"
    assert format_memory_value(2.5) == "2560m"


def test_docker_resource_args_standard_discrete_worker():
    host = {
        "hostname": "worker-1",
        "role": "worker",
        "total_ram_gb": 64.0,
        "cpus": 16,
        "unified_memory": False,
    }
    node = {
        "ram_gb": 8.0,
        "vram_gb": 12.0,  # Discrete VRAM not tracked in host ram cgroup
        "cpus": 4,
    }

    # By default on a worker, no memory ceiling is enforced (single executor under A6)
    args_default = docker_resource_args(host, node)
    assert not any(arg.startswith("--memory=") for arg in args_default)
    assert not any(arg.startswith("--memory-swap=") for arg in args_default)
    assert "--cpus=4" in args_default
    assert "--pids-limit=4096" in args_default

    # When explicitly enabled via enforce_node_memory_limit=True:
    args_enforced = docker_resource_args(host, node, enforce_node_memory_limit=True)
    assert "--memory=8g" in args_enforced
    assert "--memory-swap=8g" in args_enforced
    assert "--memory-swappiness=0" in args_enforced
    assert "--oom-score-adj=500" in args_enforced
    assert "--cpus=4" in args_enforced
    assert "--pids-limit=4096" in args_enforced


def test_docker_resource_args_unified_memory_gb10():
    # Règle cruciale GB10 : ram_gb + vram_gb doivent être couverts par --memory si activé
    host = {
        "hostname": "HEC45801",
        "role": "worker",
        "total_ram_gb": 120.0,
        "cpus": 72,
        "unified_memory": True,
    }
    node = {
        "ram_gb": 10.0,
        "vram_gb": 40.0,
        "cpus": 8,
    }

    # By default on GB10, no memory ceiling is enforced to avoid killing ECIR jobs
    args_default = docker_resource_args(host, node)
    assert not any(arg.startswith("--memory=") for arg in args_default)
    assert "--cpus=8" in args_default

    # When enforce_node_memory_limit=True, 10 RAM + 40 VRAM = 50 Go
    args_enforced = docker_resource_args(host, node, enforce_node_memory_limit=True)
    assert "--memory=50g" in args_enforced
    assert "--memory-swap=50g" in args_enforced
    assert "--memory-swappiness=0" in args_enforced
    assert "--oom-score-adj=500" in args_enforced
    assert "--cpus=8" in args_enforced
    assert "--pids-limit=4096" in args_enforced


def test_docker_resource_args_headnode_strict_ceiling():
    host = {
        "hostname": "isipol09",
        "role": "headnode",
        "total_ram_gb": 125.0,
        "cpus": 24,
        "disk_free_gb": 100.0,
        "unified_memory": False,
    }

    # Job modeste : admis
    node_ok = {
        "ram_gb": 32.0,
        "cpus": 8,
        "storage_gb": 10.0,
    }
    args = docker_resource_args(host, node_ok)
    assert "--memory=32g" in args
    assert "--memory-swap=32g" in args
    assert "--cpus=8" in args

    # Plafond CPU : si le job demande 24 CPU, il est capé à 24 - 2 = 22
    node_high_cpu = {
        "ram_gb": 10.0,
        "cpus": 24,
    }
    args_high_cpu = docker_resource_args(host, node_high_cpu)
    assert "--cpus=22" in args_high_cpu

    # Job excessif en RAM : 115 Go demandé > (125 - 16 = 109 Go) -> ValueError
    node_excessive_ram = {
        "ram_gb": 115.0,
        "cpus": 4,
    }
    with pytest.raises(ValueError, match="exceeds headnode safety ceiling"):
        docker_resource_args(host, node_excessive_ram)

    # Job excessif en disque : 85 Go demandé, reste 15 Go < réserve 20 Go -> ValueError
    node_excessive_disk = {
        "ram_gb": 10.0,
        "cpus": 4,
        "storage_gb": 85.0,
    }
    with pytest.raises(ValueError, match="violates headnode disk reserve"):
        docker_resource_args(host, node_excessive_disk)


def test_docker_resource_args_string():
    # Sur headnode : drapeaux mémoire stricts appliqués par défaut
    headnode_host = {"hostname": "isipol09", "role": "headnode", "total_ram_gb": 125.0, "cpus": 24}
    node = {"ram_gb": 2.0, "cpus": 2}
    cli_str = docker_resource_args_string(headnode_host, node)
    assert "--memory=2g" in cli_str
    assert "--memory-swap=2g" in cli_str
    assert "--memory-swappiness=0" in cli_str
    assert "--oom-score-adj=500" in cli_str
    assert "--cpus=2" in cli_str
    assert "--pids-limit=4096" in cli_str

    # Sur worker : pas de limite mémoire par défaut
    worker_host = {"hostname": "worker-1", "role": "worker", "total_ram_gb": 32.0, "cpus": 8}
    worker_cli_str = docker_resource_args_string(worker_host, node)
    assert "--memory=" not in worker_cli_str
    assert "--cpus=2" in worker_cli_str


def test_get_headnode_safe_capacities():
    raw_capacities = {
        "worker_id": "b8c303ba-f893-404e-9c06-71d866f1df5d",
        "hostname": "isipol09",
        "total_ram_gb": 125.78,
        "ram_gb": 125.78,
        "available_ram_gb": 120.0,
        "cpus": 24,
        "disk_free_gb": 216.0,
        "available_storage_gb": 216.0,
        "gpu_count": 2,
        "vram_per_gpu": [24.0, 24.0],
        "unified_memory": 0,
        "arch": "x86_64",
    }

    safe = get_headnode_safe_capacities(raw_capacities)

    # 125.78 - 16.0 = 109.78
    assert safe["ram_gb"] == 109.78
    assert safe["total_ram_gb"] == 109.78
    # 120.0 - 16.0 = 104.0
    assert safe["available_ram_gb"] == 104.0
    # 24 - 2 = 22
    assert safe["cpus"] == 22
    # 216.0 - 20.0 = 196.0
    assert safe["disk_free_gb"] == 196.0
    assert safe["role"] == "headnode"
    assert safe["is_headnode"] is True
    assert safe["placement_priority"] == 0


def test_cli_invocation(monkeypatch, capsys):
    from src.runner.host_guard import main

    # 1. Test CLI priority
    monkeypatch.setattr("sys.argv", ["host_guard.py", "--role", "headnode", "--priority"])
    main()
    captured = capsys.readouterr()
    assert captured.out.strip() == "0"

    # 2. Test CLI docker flags on headnode (enforced by default)
    monkeypatch.setattr(
        "sys.argv",
        ["host_guard.py", "--role", "headnode", "--host-profile", '{"hostname":"isipol09","total_ram_gb":125}', "--ram-gb", "4", "--cpus", "2"]
    )
    main()
    captured = capsys.readouterr()
    assert "--memory=4g" in captured.out
    assert "--memory-swap=4g" in captured.out
    assert "--memory-swappiness=0" in captured.out
    assert "--cpus=2" in captured.out

    # 3. Test CLI docker flags on worker (disabled by default)
    monkeypatch.setattr(
        "sys.argv",
        ["host_guard.py", "--role", "worker", "--host-profile", '{"hostname":"worker-1","total_ram_gb":64}', "--ram-gb", "4", "--cpus", "2"]
    )
    main()
    captured = capsys.readouterr()
    assert "--memory=" not in captured.out
    assert "--cpus=2" in captured.out

    # 4. Test CLI docker flags on worker with explicit --enforce-memory-limit
    monkeypatch.setattr(
        "sys.argv",
        ["host_guard.py", "--role", "worker", "--enforce-memory-limit", "--host-profile", '{"hostname":"worker-1","total_ram_gb":64}', "--ram-gb", "4", "--cpus", "2"]
    )
    main()
    captured = capsys.readouterr()
    assert "--memory=4g" in captured.out


def test_enforce_node_memory_limit_configuration():
    from src.config.defaults import ENFORCE_NODE_MEMORY_LIMIT, should_enforce_node_memory_limit

    # Default policy check
    assert should_enforce_node_memory_limit("headnode") is True
    assert should_enforce_node_memory_limit("HEADNODE") is True
    assert should_enforce_node_memory_limit("worker") is False
    assert should_enforce_node_memory_limit("unknown") is False

    # Host profile override
    host_worker_with_override = {
        "hostname": "worker-custom",
        "role": "worker",
        "total_ram_gb": 64.0,
        "enforce_node_memory_limit": True,
    }
    node = {"ram_gb": 4.0, "cpus": 2}
    args = docker_resource_args(host_worker_with_override, node)
    assert "--memory=4g" in args


