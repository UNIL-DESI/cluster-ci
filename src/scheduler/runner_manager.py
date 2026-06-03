import os
import subprocess
import time
import requests
import logging
import threading
import sys
import shutil
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# Logging configuration
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] slot=%(slot)s: %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)

class RunnerManager:
    def __init__(self, target_repo, github_pat, base_dir, num_slots=2):
        self.target_repo = target_repo
        self.github_pat = github_pat
        self.base_dir = Path(base_dir)
        self.runners_dir = self.base_dir / "runners"
        self.num_slots = num_slots
        self.runner_name_prefix = f"cluster-local-{target_repo.replace('/', '-')}"
        self.stop_event = threading.Event()

    def get_registration_token(self):
        """Retrieves a new registration token via the GitHub API."""
        if "/" in self.target_repo:
            api_url = f"https://api.github.com/repos/{self.target_repo}/actions/runners/registration-token"
        else:
            api_url = f"https://api.github.com/orgs/{self.target_repo}/actions/runners/registration-token"

        response = requests.post(
            api_url,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.github_pat}",
                "X-GitHub-Api-Version": "2022-11-28"
            }
        )
        response.raise_for_status()
        return response.json()["token"]

    def run_slot(self, slot_id, labels="self-hosted,cluster-ci"):
        """Manages the lifecycle of an ephemeral runner for a given slot."""
        if slot_id == "admin":
            slot_dir = self.runners_dir / "admin"
        else:
            slot_dir = self.runners_dir / f"slot{slot_id}"
        logger = logging.LoggerAdapter(logging.getLogger(__name__), {'slot': slot_id})

        # Auto-Healing: Provision slot_dir if missing
        template_dir = self.runners_dir / "template"
        if not slot_dir.exists():
            logger.info(f"Slot directory {slot_dir} is missing. Provisioning from template...")
            if not template_dir.exists():
                raise FileNotFoundError(f"Template directory {template_dir} is missing. Cannot provision slot {slot_id}.")

            # Atomic provisioning: copy to tmp then rename
            tmp_slot_dir = slot_dir.with_suffix(f".tmp_{os.getpid()}")
            try:
                if tmp_slot_dir.exists():
                    shutil.rmtree(tmp_slot_dir)
                shutil.copytree(template_dir, tmp_slot_dir)
                os.rename(tmp_slot_dir, slot_dir)
                logger.info(f"Slot directory {slot_id} provisioned atomically.")
            finally:
                if tmp_slot_dir.exists():
                    shutil.rmtree(tmp_slot_dir)

        while True:
            try:
                logger.info(f"Preparing a new ephemeral runner (labels: {labels})...")

                # 0. Clean stale runner files to prevent "already configured" errors
                #    This happens when a previous runner was deregistered from GitHub
                #    but the local files were not cleaned up (e.g. after a crash).
                for stale_file in [".runner", ".credentials", ".credentials_rsaparams"]:
                    stale_path = slot_dir / stale_file
                    if stale_path.exists():
                        stale_path.unlink()
                        logger.info(f"Removed stale file: {stale_file}")

                # 1. Obtain a token
                token = self.get_registration_token()

                # 2. Configure the runner
                runner_name = f"{self.runner_name_prefix}-slot{slot_id}"
                config_cmd = [
                    "./config.sh",
                    "--url", f"https://github.com/{self.target_repo}",
                    "--token", token,
                    "--unattended",
                    "--replace",
                    "--name", runner_name,
                    "--labels", labels,
                    "--ephemeral"
                ]

                logger.info(f"Configuring runner {runner_name}...")
                result = subprocess.run(config_cmd, cwd=slot_dir, check=True, capture_output=True, text=True)
                if result.stderr:
                    logger.debug(f"config.sh stderr: {result.stderr.strip()}")

                # 3. Launch the runner
                logger.info("Launching runner...")
                process = subprocess.Popen(["./run.sh"], cwd=slot_dir)
                
                # Active surveillance loop to detect and auto-kill zombie runners stuck in GHA cancellation loops
                cancellation_spam_count = 0
                
                while process.poll() is None:
                    time.sleep(10)
                    
                    # Scan for recent diag logs
                    diag_dir = slot_dir / "_diag"
                    if diag_dir.exists():
                        log_files = sorted(list(diag_dir.glob("Runner_*.log")), key=os.path.getmtime, reverse=True)
                        if log_files:
                            latest_log = log_files[0]
                            try:
                                with open(latest_log, "r", encoding="utf-8", errors="ignore") as f:
                                    lines = f.readlines()[-15:]
                                    cancel_lines = [l for l in lines if "Job cancellation request" in l and "received" in l]
                                    fatal_auth_lines = [l for l in lines if "Registration" in l and "was not found" in l]
                                    if len(cancel_lines) >= 8 or len(fatal_auth_lines) >= 1:
                                        cancellation_spam_count += 1
                                    else:
                                        cancellation_spam_count = max(0, cancellation_spam_count - 1)
                            except Exception:
                                pass
                    
                    if cancellation_spam_count >= 3:
                        logger.warning("🚨 [Zombie Detection] Runner is stuck in an infinite cancellation loop. Auto-healing runner process...")
                        process.terminate()
                        time.sleep(3)
                        if process.poll() is None:
                            process.kill()
                        break

                logger.info(f"Runner stopped with code {process.returncode}. Imminent restart...")

            except (subprocess.CalledProcessError, requests.RequestException) as e:
                detail = ""
                if isinstance(e, subprocess.CalledProcessError) and e.stderr:
                    detail = f" | stderr: {e.stderr.strip() if isinstance(e.stderr, str) else e.stderr.decode().strip()}"
                logger.error(f"Transient error in runner cycle: {e}{detail}. Retrying in 10s...")
                time.sleep(10)
            except Exception as e:
                logger.critical(f"Fatal error in runner cycle: {e}")
                self.stop_event.set()
                raise  # Fail-fast instead of looping on a fatal error

    def start(self):
        threads = []
        # Standard slots (staggered start to avoid config.sh race conditions)
        for i in range(1, self.num_slots + 1):
            t = threading.Thread(target=self._staggered_start, args=(i, "self-hosted,cluster-worker", i * 2))
            t.daemon = True
            t.start()
            threads.append(t)

        # Exclusive Admin slot (starts immediately)
        t_admin = threading.Thread(target=self.run_slot, args=("admin", "self-hosted,cluster-ci-admin"))
        t_admin.daemon = True
        t_admin.start()
        threads.append(t_admin)

        logger = logging.LoggerAdapter(logging.getLogger(__name__), {'slot': 'MANAGER'})
        logger.info(f"Runner manager started with {self.num_slots} slots + 1 admin slot for {self.target_repo}")

        # Keep main thread alive and monitor for fatal errors
        try:
            while not self.stop_event.is_set():
                time.sleep(1)

            if self.stop_event.is_set():
                logger.critical("Fatal error detected in a runner thread. Shutting down manager...")
                sys.exit(1)
        except KeyboardInterrupt:
            logger.info("Stopping manager...")

    def _staggered_start(self, slot_id, labels, delay):
        """Adds a delay before starting a slot to prevent concurrent config.sh collisions."""
        logger = logging.LoggerAdapter(logging.getLogger(__name__), {'slot': slot_id})
        logger.info(f"Waiting {delay}s before starting (staggered)...")
        time.sleep(delay)
        self.run_slot(slot_id, labels)

if __name__ == "__main__":
    target = os.environ.get("TARGET_REPO")
    pat = os.environ.get("GITHUB_PAT")

    if not target or not pat:
        print("Error: TARGET_REPO and GITHUB_PAT must be defined as environment variables.")
        sys.exit(1)

    # On assume que le script est dans src/scheduler/
    script_dir = Path(__file__).resolve().parent
    base_dir = script_dir.parent.parent

    manager = RunnerManager(target, pat, base_dir)
    manager.start()
