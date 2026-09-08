"""Exercise the real Bash watchdog with synthetic GPU/RAM readings, no Docker/GPU needed."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


WATCHDOG = Path(__file__).with_name("gpu_watchdog.sh")

# Commands invoked by the real watchdog. All state stays in a temporary directory;
# docker kill is recorded, never forwarded to a Docker daemon. awk runs normally
# except that /proc/meminfo is replaced with deterministic synthetic RAM readings.
MOCK_COMMAND = r'''
import json, os, pathlib, subprocess, sys
root = pathlib.Path(os.environ["WATCHDOG_FIXTURE"])
config = json.loads((root / "config.json").read_text())
tick_file = root / "tick"
tick = int(tick_file.read_text()) if tick_file.exists() else 0
name = pathlib.Path(sys.argv[0]).name
if name == "sleep":
    tick_file.write_text(str(tick + 1))
elif name == "docker":
    if sys.argv[1] == "inspect":
        print("true" if tick <= len(config["samples"]) else "false")
    elif sys.argv[1] == "kill":
        with (root / "kills.jsonl").open("a") as f:
            f.write(json.dumps(dict(tick=tick, container=sys.argv[2])) + "\n")
    else:
        raise SystemExit("Unexpected Docker operation")
elif name == "nvidia-smi":
    if "--query-gpu=memory.total" in sys.argv:
        print(config["totals"])
    elif "--query-gpu=memory.used" in sys.argv:
        print("\n".join(str(x) for x in config["samples"][tick - 1]))
    else:
        raise SystemExit("Unexpected GPU query")
elif name == "awk":
    args = sys.argv[1:]
    if "/proc/meminfo" in args:
        used = config["ram_used"][max(tick - 1, 0)] if config["ram_used"] else 0
        meminfo = root / "meminfo"
        meminfo.write_text("MemTotal: %s kB\nMemAvailable: %s kB\n" % (
            config["ram_total"] * 1024, (config["ram_total"] - used) * 1024))
        args = [str(meminfo) if a == "/proc/meminfo" else a for a in args]
    raise SystemExit(subprocess.call([os.environ["WATCHDOG_REAL_AWK"], *args]))
'''


class GPUWatchdogTests(unittest.TestCase):
    def run_watchdog(self, samples, limit=16, totals="24576\n24576", ram_used=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config.json").write_text(json.dumps(dict(
                samples=samples, totals=totals, ram_used=ram_used,
                ram_total=128 * 1024)))
            for name in ["sleep", "docker", "nvidia-smi", "awk"]:
                command = root / name
                command.write_text("#!" + sys.executable + "\n" + MOCK_COMMAND)
                command.chmod(0o755)
            env = dict(os.environ, PATH=str(root) + os.pathsep + os.environ["PATH"],
                       WATCHDOG_FIXTURE=str(root), WATCHDOG_REAL_AWK=shutil.which("awk"))
            result = subprocess.run(["bash", str(WATCHDOG), "fixture-only", str(limit)],
                                    env=env, text=True, capture_output=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(result.stderr, "")
            kills = root / "kills.jsonl"
            events = [json.loads(x) for x in kills.read_text().splitlines()] if kills.exists() else []
            self.assertTrue(all(e["container"] == "fixture-only" for e in events))
            return events, result.stdout

    def test_two_cards_within_limit_do_not_trigger_aggregate_kill(self):
        kills, _ = self.run_watchdog([[16 * 1024, 16 * 1024]] * 3, limit=24)
        self.assertEqual(kills, [])

    def test_exact_per_device_limit_is_allowed(self):
        kills, _ = self.run_watchdog([[16 * 1024, 16 * 1024]] * 3)
        self.assertEqual(kills, [])

    def test_uneven_usage_is_not_averaged(self):
        kills, output = self.run_watchdog([[24 * 1024, 8 * 1024]] * 3)
        self.assertEqual([e["tick"] for e in kills], [2])
        self.assertIn("Soft limit exceeded", output)

    def test_second_card_over_limit_is_detected(self):
        kills, _ = self.run_watchdog([[0, 17 * 1024]] * 3)
        self.assertEqual([e["tick"] for e in kills], [2])

    def test_transient_breach_resets_consecutive_counter(self):
        kills, output = self.run_watchdog([[0, n * 1024] for n in [17, 15, 17, 15]])
        self.assertEqual(kills, [])
        self.assertEqual(output.count("Memory usage back to normal"), 2)

    def test_single_gpu_below_and_above_limit(self):
        for used, expected in [(15, []), (17, [2])]:
            with self.subTest(used=used):
                kills, _ = self.run_watchdog([[used * 1024]] * 3, totals="24576")
                self.assertEqual([e["tick"] for e in kills], expected)

    def test_unified_memory_below_limit(self):
        kills, output = self.run_watchdog([[]] * 3, limit=100, totals="[N/A]",
                                          ram_used=[90 * 1024] * 3)
        self.assertEqual(kills, [])
        self.assertIn("Mode: system-ram", output)

    def test_unified_memory_soft_limit_still_enforced(self):
        kills, output = self.run_watchdog([[]] * 3, limit=100, totals="[N/A]",
                                          ram_used=[101 * 1024] * 3)
        self.assertEqual([e["tick"] for e in kills], [2])
        self.assertIn("Soft limit exceeded", output)

    def test_unified_memory_hard_limit_kills_immediately(self):
        kills, output = self.run_watchdog([[]] * 3, limit=120, totals="[N/A]",
                                          ram_used=[116 * 1024] * 3)
        self.assertEqual([e["tick"] for e in kills], [1])
        self.assertIn("HARD LIMIT BREACHED", output)


if __name__ == "__main__":
    unittest.main()
