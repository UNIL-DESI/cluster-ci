"""Unit tests for Host Guard module (Cluster-CI v3 W11)."""

import pytest
from src.runner.host_guard import (
    docker_resource_args,
    docker_resource_args_string,
    format_memory_value,
    get_headnode_safe_capacities,
    is_headnode_host,
    is_unified_memory_host,
    placement_priority,
)


def test_is_headnode_detection(monkeypatch):
    # Via flag explicite
    assert is_headnode_host({"is_headnode": True}) is True
    assert is_headnode_host({"is_headnode": False}) is False

    # Via role
    assert is_headnode_host({"role": "headnode"}) is True
    assert is_headnode_host({"role": "HEADNODE"}) is True
    assert is_headnode_host({"role": "headnode_worker"}) is True
    assert is_headnode_host({"role": "worker"}) is False

    # A14: Zero hardcoded hostnames/IPs - hostname alone does NOT identify a headnode
    assert is_headnode_host({"hostname": "isipol09"}) is False
    assert is_headnode_host({"hostname": "ISIPOL09"}) is False
    assert is_headnode_host({"hostname": "HEC45801"}) is False

    # Via environment variable CLUSTER_CI_ROLE / IS_HEADNODE
    monkeypatch.setenv("CLUSTER_CI_ROLE", "headnode")
    assert is_headnode_host({}) is True
    assert is_headnode_host({"hostname": "any-node"}) is True
    # Explicit role="worker" overrides local env default
    assert is_headnode_host({"role": "worker"}) is False
    monkeypatch.delenv("CLUSTER_CI_ROLE", raising=False)

    monkeypatch.setenv("IS_HEADNODE", "1")
    assert is_headnode_host({}) is True
    monkeypatch.delenv("IS_HEADNODE", raising=False)


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

    # Amendement A12: unconditional memory limits on all machines
    args = docker_resource_args(host, node)
    assert "--memory=8g" in args
    assert "--memory-swap=8g" in args
    assert "--memory-swappiness=0" in args
    assert "--oom-score-adj=500" in args
    assert "--cpus=4" in args
    assert "--pids-limit=4096" in args
    # Not headnode, so no cgroup-parent
    assert not any(arg.startswith("--cgroup-parent=") for arg in args)


def test_docker_resource_args_unified_memory_gb10():
    # Règle cruciale GB10 : ram_gb + vram_gb doivent être couverts par --memory (Amendement A12)
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

    args = docker_resource_args(host, node)
    assert "--memory=50g" in args
    assert "--memory-swap=50g" in args
    assert "--memory-swappiness=0" in args
    assert "--oom-score-adj=500" in args
    assert "--cpus=8" in args
    assert "--pids-limit=4096" in args
    assert not any(arg.startswith("--cgroup-parent=") for arg in args)


def test_docker_resource_args_headnode_strict_ceiling():
    host = {
        "hostname": "isipol09",
        "role": "headnode",
        "total_ram_gb": 125.0,
        "cpus": 24,
        "disk_free_gb": 100.0,
        "unified_memory": False,
        "verify_cgroup": False,
    }

    # Job modeste : admis, avec cgroup parent par défaut /cluster-jobs (A11)
    node_ok = {
        "ram_gb": 32.0,
        "cpus": 8,
        "storage_gb": 10.0,
    }
    args = docker_resource_args(host, node_ok)
    assert "--memory=32g" in args
    assert "--memory-swap=32g" in args
    assert "--cpus=8" in args
    assert "--cgroup-parent=/cluster-jobs" in args

    # Custom cgroup parent
    host_custom = dict(host, cgroup_parent="/cluster-jobs.slice")
    args_custom = docker_resource_args(host_custom, node_ok)
    assert "--cgroup-parent=/cluster-jobs.slice" in args_custom

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
    # Sur headnode : drapeaux mémoire stricts et cgroup parent
    headnode_host = {"role": "headnode", "total_ram_gb": 125.0, "cpus": 24, "verify_cgroup": False}
    node = {"ram_gb": 2.0, "cpus": 2}
    cli_str = docker_resource_args_string(headnode_host, node)
    assert "--memory=2g" in cli_str
    assert "--memory-swap=2g" in cli_str
    assert "--memory-swappiness=0" in cli_str
    assert "--oom-score-adj=500" in cli_str
    assert "--cpus=2" in cli_str
    assert "--pids-limit=4096" in cli_str
    assert "--cgroup-parent=/cluster-jobs" in cli_str

    # Sur worker : drapeaux mémoire stricts sans cgroup-parent
    worker_host = {"role": "worker", "total_ram_gb": 32.0, "cpus": 8}
    worker_cli_str = docker_resource_args_string(worker_host, node)
    assert "--memory=2g" in worker_cli_str
    assert "--memory-swap=2g" in worker_cli_str
    assert "--memory-swappiness=0" in worker_cli_str
    assert "--cpus=2" in worker_cli_str
    assert "--cgroup-parent=" not in worker_cli_str


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

    # 2. Test CLI docker flags on headnode (unconditional limits + cgroup parent)
    monkeypatch.setattr(
        "sys.argv",
        ["host_guard.py", "--role", "headnode", "--host-profile", '{"total_ram_gb":125,"verify_cgroup":false}', "--ram-gb", "4", "--cpus", "2"]
    )
    main()
    captured = capsys.readouterr()
    assert "--memory=4g" in captured.out
    assert "--memory-swap=4g" in captured.out
    assert "--memory-swappiness=0" in captured.out
    assert "--cpus=2" in captured.out
    assert "--cgroup-parent=/cluster-jobs" in captured.out

    # 3. Test CLI docker flags on worker (unconditional limits, no cgroup parent)
    monkeypatch.setattr(
        "sys.argv",
        ["host_guard.py", "--role", "worker", "--host-profile", '{"total_ram_gb":64}', "--ram-gb", "4", "--cpus", "2"]
    )
    main()
    captured = capsys.readouterr()
    assert "--memory=4g" in captured.out
    assert "--memory-swap=4g" in captured.out
    assert "--memory-swappiness=0" in captured.out
    assert "--cpus=2" in captured.out
    assert "--cgroup-parent=" not in captured.out

    # 4. Test CLI custom cgroup parent
    monkeypatch.setattr(
        "sys.argv",
        ["host_guard.py", "--role", "headnode", "--cgroup-parent", "/cluster-custom", "--host-profile", '{"total_ram_gb":125,"verify_cgroup":false}', "--ram-gb", "4", "--cpus", "2"]
    )
    main()
    captured = capsys.readouterr()
    assert "--cgroup-parent=/cluster-custom" in captured.out


def test_defaults_constants():
    from src.config.defaults import (
        DEFAULT_HEADNODE_CGROUP_PARENT,
        DEFAULT_HEADNODE_CPU_RESERVE,
        DEFAULT_HEADNODE_DISK_RESERVE_GB,
        DEFAULT_HEADNODE_RAM_RESERVE_GB,
        DEFAULT_PLACEMENT_PRIORITY,
        HEADNODE_PLACEMENT_PRIORITY,
    )

    assert DEFAULT_HEADNODE_CGROUP_PARENT == "/cluster-jobs"
    assert DEFAULT_HEADNODE_RAM_RESERVE_GB == 16.0
    assert DEFAULT_HEADNODE_CPU_RESERVE == 2
    assert DEFAULT_HEADNODE_DISK_RESERVE_GB == 20.0
    assert DEFAULT_PLACEMENT_PRIORITY == 50
    assert HEADNODE_PLACEMENT_PRIORITY == 0


def test_cgroup_refusal_when_unlimited_or_missing():
    from src.runner.host_guard import check_cgroup_memory_limit

    # 1. Cgroup manquant
    valid, reason, _ = check_cgroup_memory_limit("/nonexistent-cgroup-xyz")
    assert valid is False
    assert "introuvable" in reason

    # 2. Refus dans docker_resource_args si enforce_cgroup_check=True
    host_profile = {
        "role": "headnode",
        "enforce_cgroup_check": True,
        "cgroup_parent": "/nonexistent-cgroup-xyz",
    }
    with pytest.raises(ValueError, match="Refus de production --cgroup-parent"):
        docker_resource_args(host_profile, {"ram_gb": 4})


def test_docker_resource_args_gpus_and_shm():
    host = {"role": "worker", "total_ram_gb": 64}

    # gpus = 0 -> pas de --gpus
    args_0 = docker_resource_args(host, {"ram_gb": 8, "gpus": 0})
    assert not any(a.startswith("--gpus") for a in args_0)
    assert "--shm-size=2g" in args_0

    # gpus = 1 avec device ids
    args_1 = docker_resource_args(host, {"ram_gb": 16, "gpus": 1, "gpu_ids": [0]})
    assert '--gpus="device=0"' in args_1
    assert "--shm-size=4g" in args_1

    # gpus = 2 sans device ids -> fail-fast (ValueError)
    with pytest.raises(ValueError, match="requested but no gpu_ids assigned"):
        docker_resource_args(host, {"ram_gb": 16, "gpus": 2})

    # gpus = 2 avec device ids -> --gpus="device=0,1"
    args_2 = docker_resource_args(host, {"ram_gb": 16, "gpus": 2, "gpu_ids": [0, 1]})
    assert '--gpus="device=0,1"' in args_2


def test_docker_resource_args_pytorch_cuda_alloc_conf_injection():
    # 1. Sur hôte unified memory (Grace-Blackwell GB10) : injection obligatoire
    gb10_host = {
        "hostname": "HEC45801",
        "role": "worker",
        "unified_memory": True,
        "total_ram_gb": 120.0,
    }
    args_gb10 = docker_resource_args(gb10_host, {"ram_gb": 16.0})
    assert "-e PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True" in args_gb10

    # 2. Sur hôte discrete standard avec GPU demandé : injection présente
    discrete_host = {"role": "worker", "total_ram_gb": 64.0, "unified_memory": False}
    args_gpu = docker_resource_args(discrete_host, {"ram_gb": 16.0, "gpus": 1, "gpu_ids": [0]})
    assert "-e PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True" in args_gpu

    # 3. Sur hôte discrete standard CPU-only : pas d'injection PyTorch CUDA
    args_cpu = docker_resource_args(discrete_host, {"ram_gb": 8.0, "gpus": 0})
    assert "-e PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True" not in args_cpu

    # 4. Respect de la configuration utilisateur : NE PAS écraser une valeur personnalisée
    # Cas A: spécifiée via dict env dans node_resources
    args_user_env = docker_resource_args(
        gb10_host,
        {"ram_gb": 16.0, "env": {"PYTORCH_CUDA_ALLOC_CONF": "max_split_size_mb:128"}},
    )
    assert "-e PYTORCH_CUDA_ALLOC_CONF=max_split_size_mb:128" in args_user_env
    assert "-e PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True" not in args_user_env

    # Cas B: spécifiée via clé directe pytorch_cuda_alloc_conf
    args_user_direct = docker_resource_args(
        discrete_host,
        {"ram_gb": 16.0, "gpus": 1, "gpu_ids": [0], "pytorch_cuda_alloc_conf": "garbage_collection_threshold:0.8"},
    )
    assert "-e PYTORCH_CUDA_ALLOC_CONF=garbage_collection_threshold:0.8" in args_user_direct
    assert "-e PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True" not in args_user_direct


def test_cli_watchdog_invocation(monkeypatch, tmp_path):
    from src.runner.host_guard import main

    marker_file = tmp_path / "marker.json"

    # Invoque main() avec --watchdog
    monkeypatch.setattr(
        "sys.argv",
        [
            "host_guard.py",
            "--watchdog",
            "mock-test-container",
            "--reserve-gb",
            "12.0",
            "--poll-interval",
            "0.01",
            "--marker-file",
            str(marker_file),
        ],
    )
    # Monkeypatch subprocess.run pour déléguer sans lancer bash réel
    monkeypatch.setattr(
        "subprocess.run",
        lambda *args, **kwargs: type("Proc", (), {"returncode": 0})(),
    )

    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 0


def test_run_host_memory_watchdog_vram_zero_command(monkeypatch):
    """Vérifie que run_host_memory_watchdog transmet bien 0 pour désactiver le soft limit VRAM."""
    from src.runner.host_guard import run_host_memory_watchdog

    captured_cmd = []

    def mock_run(cmd, env=None):
        captured_cmd.extend(cmd)
        return type("Proc", (), {"returncode": 0})()

    monkeypatch.setattr("subprocess.run", mock_run)
    ret = run_host_memory_watchdog("test-container", vram_limit_gb=0.0)
    assert ret == 0
    assert captured_cmd[-2:] == ["test-container", "0"]


def test_gpu_watchdog_zero_vram_limit_does_not_kill_container(tmp_path):
    """Test de non-régression: vram_limit_gb=0 ne doit pas tuer le conteneur dès 1 MiB."""
    import shutil
    import subprocess
    import sys
    from pathlib import Path

    bash_bin = shutil.which("bash")
    if not bash_bin or sys.platform == "win32":
        pytest.skip("Bash/Linux requis pour l'exécution directe de gpu_watchdog.sh")

    if not Path("/proc/meminfo").is_file():
        pytest.skip("/proc/meminfo absent sur ce système")

    mock_bin = tmp_path / "bin"
    mock_bin.mkdir()

    # Mock docker: inspect returns true, kill records call
    (mock_bin / "docker").write_text("#!/bin/bash\nif [[ \"$*\" == *\"State.Running\"* ]]; then echo \"true\"; exit 0; fi\nexit 0\n")
    (mock_bin / "docker").chmod(0o755)

    # Mock nvidia-smi: total 16GB, used 100MB
    (mock_bin / "nvidia-smi").write_text("#!/bin/bash\nif [[ \"$*\" == *\"memory.total\"* ]]; then echo \"16384\"; elif [[ \"$*\" == *\"memory.used\"* ]]; then echo \"100\"; fi\nexit 0\n")
    (mock_bin / "nvidia-smi").chmod(0o755)

    marker_file = tmp_path / "host_guard_killed.marker"
    watchdog_script = Path(__file__).parent.parent / "src" / "runner" / "gpu_watchdog.sh"

    env = dict(
        PATH=f"{mock_bin}:{sys.path}",
        WATCHDOG_POLL_INTERVAL="0.05",
        HOST_GUARD_MARKER_FILE=str(marker_file),
    )

    proc = subprocess.Popen(
        ["bash", str(watchdog_script), "test-container", "0"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    try:
        # Laisser tourner 0.2s (plusieurs vérifications)
        proc.wait(timeout=0.2)
    except subprocess.TimeoutExpired:
        # Le watchdog tourne toujours sans tuer le conteneur: comportement attendu
        proc.terminate()
        proc.wait(timeout=1)

    assert not marker_file.exists(), "Le marqueur ne doit pas être créé lorsque vram_limit_gb=0 !"


def test_purge_host_guard_marker(tmp_path):
    """Vérifie la purge du marqueur host_guard_killed.marker dans le repo et le répertoire courant."""
    from src.runner.host_guard import purge_host_guard_marker

    ws = tmp_path / "workspace"
    ws.mkdir()
    marker_in_ws = ws / "host_guard_killed.marker"
    marker_in_ws.write_text('{"status": "killed"}')

    assert marker_in_ws.exists()
    purged = purge_host_guard_marker(workspace_dir=ws)
    assert purged is True
    assert not marker_in_ws.exists()

    # Idempotent: second call returns False without raising
    assert purge_host_guard_marker(workspace_dir=ws) is False


def test_wait_for_job_host_memory_guard_detection(capsys, monkeypatch):
    """Vérifie que submit_job détecte Host Memory Guard via failure_reason structuré ou logs accumulés."""
    from unittest.mock import Mock
    from src.scheduler import submit_job

    # Cas 1 : failure_reason structuré au niveau du nœud DAG
    job_status_resp = Mock()
    job_status_resp.status_code = 200
    job_status_resp.json.return_value = {
        "status": "failed",
        "exit_code": 137,
        "nodes": [
            {
                "name": "train",
                "status": "failed",
                "failure_reason": "HostMemoryPressureExceeded",
                "error_message": "HostMemoryPressureExceeded: Container killed",
            }
        ],
    }

    logs_resp = Mock()
    logs_resp.status_code = 200
    logs_resp.json.return_value = {"logs": "", "offset": 0}

    def mock_get(url, *args, **kwargs):
        if "job_status" in url:
            return job_status_resp
        return logs_resp

    monkeypatch.setattr(submit_job.requests, "get", mock_get)

    code = submit_job.wait_for_job("http://127.0.0.1:8000", "job-test-1")
    assert code == 137
    captured = capsys.readouterr()
    assert "Job was terminated by Host Memory Guard" in captured.out
    assert "OOM Killer" not in captured.out

    # Cas 2 : Standard OOM (pas de Host Memory Guard)
    job_status_resp.json.return_value = {
        "status": "failed",
        "exit_code": 137,
        "nodes": [{"name": "train", "status": "failed", "error_message": "Exit code 137"}],
    }
    submit_job.wait_for_job("http://127.0.0.1:8000", "job-test-2")
    captured = capsys.readouterr()
    assert "killed by system (OOM Killer)" in captured.out
    assert "Host Memory Guard" not in captured.out

    # Cas 3 : Host Memory Guard vu dans un chunk de log antérieur (pas dans le dernier chunk vide)
    chunk1_logs = Mock()
    chunk1_logs.status_code = 200
    chunk1_logs.json.return_value = {"logs": "❌ Error: Host Memory Guard triggered\n", "offset": 40}

    chunk2_logs = Mock()
    chunk2_logs.status_code = 200
    chunk2_logs.json.return_value = {"logs": "", "offset": 40}

    status_running = Mock()
    status_running.status_code = 200
    status_running.json.return_value = {"status": "running"}

    status_failed = Mock()
    status_failed.status_code = 200
    status_failed.json.return_value = {"status": "failed", "exit_code": 137}

    call_count = {"status": 0, "logs": 0}

    def mock_get_multi(url, *args, **kwargs):
        if "job_status" in url:
            call_count["status"] += 1
            if call_count["status"] == 1:
                return status_running
            return status_failed
        else:
            call_count["logs"] += 1
            if call_count["logs"] == 1:
                return chunk1_logs
            return chunk2_logs

    monkeypatch.setattr(submit_job.requests, "get", mock_get_multi)
    monkeypatch.setattr(submit_job.time, "sleep", lambda s: None)

    code = submit_job.wait_for_job("http://127.0.0.1:8000", "job-test-3")
    assert code == 137
    captured = capsys.readouterr()
    assert "Job was terminated by Host Memory Guard" in captured.out
    assert "killed by system (OOM Killer)" not in captured.out






