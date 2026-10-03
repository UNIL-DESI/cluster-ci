import os
import sys
import unittest
from unittest.mock import patch, MagicMock
import tempfile
import shutil

from src.scheduler.worker_agent import (
    parse_nvidia_smi_output,
    get_cpu_info,
    get_arch_info,
    get_storage_info,
    get_worker_capabilities,
    build_registration_payload,
    execute_job,
    app,
    WORKER_ID
)
import src.scheduler.worker_agent as worker_agent

class TestWorkerCapacities(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        worker_agent.REPOS_DIR = self.test_dir
        self.app = app.test_client()
        self.app.testing = True

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_detection_gb10_unified_memory(self):
        """Test Case 1: NVIDIA GB10 Grace-Blackwell with [N/A] reported by nvidia-smi.
        Should detect unified memory, report system RAM as VRAM, and set unified_memory=1.
        """
        # Recorded output of nvidia-smi for Grace-Blackwell GB10
        gb10_output = "0, NVIDIA GB10, [N/A], [N/A], [N/A]"
        total_ram = 120.0
        avail_ram = 112.5

        data = parse_nvidia_smi_output(
            gb10_output,
            total_ram_gb=total_ram,
            available_ram_gb=avail_ram
        )

        self.assertEqual(data["gpu_count"], 1)
        self.assertEqual(data["gpu_name"], "NVIDIA GB10")
        self.assertEqual(data["unified_memory"], 1)
        self.assertEqual(data["total_vram_gb"], 120.0)
        self.assertEqual(data["available_vram_gb"], 112.5)
        self.assertEqual(data["vram_per_gpu"], [120.0])
        self.assertEqual(len(data["gpu_details"]), 1)
        self.assertEqual(data["gpu_details"][0]["unified_memory"], 1)

    def test_detection_gb10_variant_not_supported(self):
        """Test GB10 with [Not Supported] text variation."""
        gb10_output = "NVIDIA Grace Blackwell GB10, [Not Supported], [Not Supported]"
        total_ram = 128.0
        avail_ram = 100.0

        data = parse_nvidia_smi_output(
            gb10_output,
            total_ram_gb=total_ram,
            available_ram_gb=avail_ram
        )

        self.assertEqual(data["gpu_count"], 1)
        self.assertEqual(data["unified_memory"], 1)
        self.assertEqual(data["total_vram_gb"], 128.0)
        self.assertEqual(data["vram_per_gpu"], [128.0])

    def test_detection_dual_rtx3090_discrete(self):
        """Test Case 2: 2x NVIDIA GeForce RTX 3090 (discrete GPUs).
        Should detect 2 GPUs, discrete memory (unified_memory=0), per-GPU VRAM of 24 GB,
        vram_per_gpu=[24.0, 24.0], and available VRAM as minimum free.
        """
        # Recorded output of nvidia-smi for 2x RTX 3090 (isipol09)
        rtx3090_output = (
            "0, NVIDIA GeForce RTX 3090, 24576, 1024, 23552\n"
            "1, NVIDIA GeForce RTX 3090, 24576, 512, 24064"
        )

        data = parse_nvidia_smi_output(rtx3090_output)

        self.assertEqual(data["gpu_count"], 2)
        self.assertEqual(data["gpu_name"], "2x NVIDIA GeForce RTX 3090")
        self.assertEqual(data["unified_memory"], 0)
        self.assertEqual(data["total_vram_gb"], 24.0)
        self.assertEqual(data["available_vram_gb"], 23.0)  # min free: 23552 / 1024 = 23.0
        self.assertEqual(data["vram_per_gpu"], [24.0, 24.0])
        self.assertEqual(len(data["gpu_details"]), 2)
        self.assertEqual(data["gpu_details"][0]["index"], 0)
        self.assertEqual(data["gpu_details"][1]["index"], 1)
        self.assertEqual(data["gpu_details"][0]["unified_memory"], 0)

    def test_detection_no_gpu(self):
        """Test Case 3: Host with no GPU (CPU-only / nvidia-smi missing).
        Should return 0 GPUs, N/A, unified_memory=0, and empty vram_per_gpu.
        """
        data = parse_nvidia_smi_output("")

        self.assertEqual(data["gpu_count"], 0)
        self.assertEqual(data["gpu_name"], "N/A")
        self.assertEqual(data["unified_memory"], 0)
        self.assertEqual(data["total_vram_gb"], 0.0)
        self.assertEqual(data["available_vram_gb"], 0.0)
        self.assertEqual(data["vram_per_gpu"], [])
        self.assertEqual(data["gpu_details"], [])

    def test_unified_memory_env_override(self):
        """Test CLUSTER_CI_UNIFIED_MEMORY env var override."""
        # Force unified on discrete GPU
        with patch.dict(os.environ, {"CLUSTER_CI_UNIFIED_MEMORY": "1"}):
            rtx_output = "0, NVIDIA GeForce RTX 3090, 24576, 1024, 23552"
            data = parse_nvidia_smi_output(rtx_output, total_ram_gb=64.0, available_ram_gb=60.0)
            self.assertEqual(data["unified_memory"], 1)
            self.assertEqual(data["total_vram_gb"], 64.0)

        # Force discrete on GB10
        with patch.dict(os.environ, {"CLUSTER_CI_UNIFIED_MEMORY": "0"}):
            gb10_output = "0, NVIDIA GB10, 122880, 2048, 120832"
            data = parse_nvidia_smi_output(gb10_output)
            self.assertEqual(data["unified_memory"], 0)
            self.assertEqual(data["total_vram_gb"], 120.0)

    def test_cpu_detection_cgroups_v2(self):
        """Test cgroups v2 quota/period detection."""
        fake_content = "400000 100000\n"  # 4 CPUs
        with patch("os.path.exists", side_effect=lambda p: p == "/sys/fs/cgroup/cpu.max"):
            with patch("builtins.open", unittest.mock.mock_open(read_data=fake_content)):
                with patch("os.cpu_count", return_value=16):
                    cpus = get_cpu_info()
                    self.assertEqual(cpus, 4)

    def test_cpu_detection_cgroups_v1(self):
        """Test cgroups v1 quota/period detection."""
        def fake_exists(p):
            return p in ("/sys/fs/cgroup/cpu/cpu.cfs_quota_us", "/sys/fs/cgroup/cpu/cpu.cfs_period_us")

        def fake_open(p, *args, **kwargs):
            if "quota" in p:
                return unittest.mock.mock_open(read_data="200000\n")()
            elif "period" in p:
                return unittest.mock.mock_open(read_data="100000\n")()
            raise FileNotFoundError(p)

        with patch("os.path.exists", side_effect=fake_exists):
            with patch("builtins.open", side_effect=fake_open):
                with patch("os.cpu_count", return_value=16):
                    cpus = get_cpu_info()
                    self.assertEqual(cpus, 2)

    def test_cpu_detection_env_override(self):
        """Test CLUSTER_CI_CPUS environment variable override."""
        with patch.dict(os.environ, {"CLUSTER_CI_CPUS": "8"}):
            cpus = get_cpu_info()
            self.assertEqual(cpus, 8)

    def test_arch_detection(self):
        """Test architecture detection and override."""
        arch = get_arch_info()
        self.assertIsInstance(arch, str)
        self.assertGreater(len(arch), 0)

        with patch.dict(os.environ, {"CLUSTER_CI_ARCH": "aarch64"}):
            self.assertEqual(get_arch_info(), "aarch64")

    def test_registration_payload_retrocompatible_and_v3_fields(self):
        """Verify registration/heartbeat payload contains ALL existing fields
        plus ALL required v3 fields (cpus, ram_gb, vram_per_gpu, unified_memory, arch, disk_free_gb).
        """
        payload = build_registration_payload(is_startup=True)

        # 1. Existing backward-compatible fields
        existing_keys = [
            "worker_id", "hostname", "service_url", "total_ram_gb",
            "available_ram_gb", "total_storage_gb", "available_storage_gb",
            "total_vram_gb", "gpu_count", "gpu_name", "available_vram_gb",
            "is_startup"
        ]
        for key in existing_keys:
            self.assertIn(key, payload, f"Missing existing field {key}")

        self.assertTrue(payload["is_startup"])

        # 2. New v3 capacity fields
        v3_keys = [
            "cpus", "ram_gb", "vram_per_gpu", "unified_memory",
            "arch", "disk_free_gb"
        ]
        for key in v3_keys:
            self.assertIn(key, payload, f"Missing v3 capacity field {key}")

        # Check types
        self.assertIsInstance(payload["cpus"], int)
        self.assertIsInstance(payload["ram_gb"], float)
        self.assertIsInstance(payload["vram_per_gpu"], list)
        self.assertIn(payload["unified_memory"], (0, 1))
        self.assertIsInstance(payload["arch"], str)
        self.assertIsInstance(payload["disk_free_gb"], float)

    @patch("subprocess.Popen")
    @patch("src.scheduler.worker_agent.safe_docker_rm_f")
    @patch("src.scheduler.worker_agent.update_job_status")
    @patch("src.scheduler.worker_agent.purge_orphan_runners_and_containers")
    def test_parallel_mode_execution_env_vars(self, mock_purge, mock_status, mock_docker_rm, mock_popen):
        """Verify execution in parallel_mode sets CLUSTER_CI_PARALLEL_MODE=1,
        CLUSTER_CI_RUNNER_ID, CLUSTER_CI_JOB_ID, and HEADNODE_URL.
        """
        # Configure mock process
        mock_proc = MagicMock()
        mock_proc.poll.side_effect = [0]
        mock_proc.wait.return_value = 0
        mock_proc.stdout = []
        mock_popen.return_value = mock_proc

        job = {
            "job_id": "test-parallel-job-123",
            "repo": "user/repo",
            "branch": "feat/parallel",
            "ram_required_gb": 16.0,
            "parallel_mode": 1,
            "role": "executor",
            "runner_id": "runner-node-alpha"
        }

        execute_job(job)

        self.assertTrue(mock_popen.called)
        _, kwargs = mock_popen.call_args_list[0]
        env = kwargs.get("env", {})

        self.assertEqual(env.get("CLUSTER_CI_PARALLEL_MODE"), "1")
        self.assertEqual(env.get("CLUSTER_CI_RUNNER_ID"), "runner-node-alpha")
        self.assertEqual(env.get("CLUSTER_CI_JOB_ID"), "test-parallel-job-123")
        self.assertIn("HEADNODE_URL", env)
        self.assertIn("CLUSTER_CI_HEADNODE_URL", env)
        self.assertEqual(env.get("CLUSTER_CI_WORKER_ID"), WORKER_ID)
        self.assertEqual(env.get("CLUSTER_CI_ROLE"), "executor")

    @patch("subprocess.Popen")
    @patch("src.scheduler.worker_agent.safe_docker_rm_f")
    @patch("src.scheduler.worker_agent.update_job_status")
    @patch("src.scheduler.worker_agent.purge_orphan_runners_and_containers")
    def test_classic_mode_execution_unmodified(self, mock_purge, mock_status, mock_docker_rm, mock_popen):
        """Verify execution in classic mode does NOT set CLUSTER_CI_PARALLEL_MODE."""
        mock_proc = MagicMock()
        mock_proc.poll.side_effect = [0]
        mock_proc.wait.return_value = 0
        mock_proc.stdout = []
        mock_popen.return_value = mock_proc

        job = {
            "job_id": "test-classic-job-456",
            "repo": "user/repo",
            "branch": "main",
            "ram_required_gb": 8.0
        }

        execute_job(job)

        self.assertTrue(mock_popen.called)
        _, kwargs = mock_popen.call_args_list[0]
        env = kwargs.get("env", {})

        self.assertNotIn("CLUSTER_CI_PARALLEL_MODE", env)
        self.assertNotIn("CLUSTER_CI_RUNNER_ID", env)
        self.assertEqual(env.get("CLUSTER_CI_MODE"), "executor")
        self.assertEqual(env.get("JOB_ID"), "test-classic-job-456")

    @patch("src.scheduler.worker_agent._async_job_cleanup")
    def test_cancel_active_job_local_cleanup(self, mock_async_cleanup):
        """Verify POST /cancel/<job_id> stops active local process cleanly."""
        job_id = "job-to-cancel-789"
        mock_proc = MagicMock()
        mock_proc.pid = 99999

        with worker_agent.job_lock:
            worker_agent.current_job_id = job_id
            worker_agent.current_process = mock_proc

        resp = self.app.post(f"/cancel/{job_id}")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["status"], "cancelled")

        # Verify active job tracker reset immediately
        self.assertIsNone(worker_agent.current_job_id)
        self.assertIsNone(worker_agent.current_process)

        # Verify cleanup thread launched
        self.assertTrue(mock_async_cleanup.called)
        args, _ = mock_async_cleanup.call_args
        self.assertEqual(args[0], job_id)
        self.assertEqual(args[2], mock_proc)

    def test_cancel_nonexistent_job_returns_404(self):
        """Verify POST /cancel/<job_id> for unknown job with no containers returns 404."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="")
            resp = self.app.post("/cancel/nonexistent-job")
            self.assertEqual(resp.status_code, 404)
