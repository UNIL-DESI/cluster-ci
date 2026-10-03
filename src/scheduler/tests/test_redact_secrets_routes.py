"""Integration tests verifying secret redaction on API routes and internal worker transmission."""

import os
import sys
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import json
import uuid
import pytest
from unittest.mock import patch, MagicMock

# Isolated test DB
test_db = f"test_redact_{uuid.uuid4().hex[:8]}.db"
os.environ["CLUSTER_DB_PATH"] = test_db
os.environ["CLUSTER_TOKEN"] = "test-cluster-token"
os.environ["HEADNODE_URL"] = "http://127.0.0.1:5000"

from headnode_service import app, init_db, get_db_conn
from worker_agent import app as worker_app


FAKE_GH_TOKEN = "ghp_TESTTESTTEST99999999999999999999"
FAKE_HF_TOKEN = "hf_SUPERSECRET1234567890abcdef"
FAKE_TOGETHER_KEY = "tgp_TOGETHERKEY987654321fedcba"


@pytest.fixture
def client():
    with app.test_client() as client:
        with app.app_context():
            init_db()
            yield client
    if os.path.exists(test_db):
        try:
            os.remove(test_db)
        except Exception:
            pass


@pytest.fixture
def worker_client():
    with worker_app.test_client() as client:
        yield client


def insert_test_job(status='running', worker_id='worker1'):
    job_id = f"job-{uuid.uuid4().hex[:8]}"
    raw_env_vars = {
        "HF_TOKEN": FAKE_HF_TOKEN,
        "TOGETHER_API_KEY": FAKE_TOGETHER_KEY,
        "BATCH_SIZE": "64"
    }

    with get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR IGNORE INTO workers (worker_id, hostname, service_url, total_ram_gb, status, last_seen)
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ''', (worker_id, 'node1', 'http://127.0.0.1:6000', 64.0, 'online'))

        cursor.execute('''
            INSERT INTO jobs (
                job_id, repo, branch, commit_hash, ram_required_gb, vram_required_gb,
                max_runtime_hours, gh_token, env_vars, username, status, worker_id,
                created_at, started_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        ''', (
            job_id, "UNIL-DESI/test-repo", "main", "c0ffee1234567890", 16.0, 0.0,
            2.0, FAKE_GH_TOKEN, json.dumps(raw_env_vars), "tester", status, worker_id
        ))
        conn.commit()

    return job_id


def test_job_status_redacts_secrets(client):
    """Verify /job_status/<job_id> masks gh_token and env_vars while keeping variable names."""
    job_id = insert_test_job(status='running')

    resp = client.get(f"/job_status/{job_id}")
    assert resp.status_code == 200
    text = resp.get_data(as_text=True)

    # 1. No secret pattern leaked
    assert FAKE_GH_TOKEN not in text
    assert FAKE_HF_TOKEN not in text
    assert FAKE_TOGETHER_KEY not in text

    # 2. Key names preserved, values masked
    data = resp.get_json()
    assert data["gh_token"] == "***REDACTED***"
    parsed_env = json.loads(data["env_vars"])
    assert set(parsed_env.keys()) == {"HF_TOKEN", "TOGETHER_API_KEY", "BATCH_SIZE"}
    assert parsed_env["HF_TOKEN"] == "***REDACTED***"
    assert parsed_env["TOGETHER_API_KEY"] == "***REDACTED***"
    assert parsed_env["BATCH_SIZE"] == "***REDACTED***"


def test_api_runs_active_redacts_secrets(client):
    """Verify /api/runs/active masks gh_token and env_vars for authenticated sessions."""
    job_id = insert_test_job(status='running')

    with client.session_transaction() as sess:
        sess['user'] = {'login': 'tester'}

    resp = client.get("/api/runs/active")
    assert resp.status_code == 200
    text = resp.get_data(as_text=True)

    assert FAKE_GH_TOKEN not in text
    assert FAKE_HF_TOKEN not in text
    assert FAKE_TOGETHER_KEY not in text

    data = resp.get_json()
    assert isinstance(data, list)
    matching = [j for j in data if j["job_id"] == job_id]
    assert len(matching) == 1
    target = matching[0]
    assert target["gh_token"] == "***REDACTED***"
    parsed_env = json.loads(target["env_vars"])
    assert set(parsed_env.keys()) == {"HF_TOKEN", "TOGETHER_API_KEY", "BATCH_SIZE"}
    assert parsed_env["HF_TOKEN"] == "***REDACTED***"


def test_internal_worker_poll_transmits_secrets_intact(client):
    """Verify /worker_poll/<worker_id> leaves secrets intact for worker container execution."""
    worker_id = f"worker-{uuid.uuid4().hex[:6]}"
    job_id = insert_test_job(status='assigned', worker_id=worker_id)

    headers = {"Authorization": f"Bearer {os.environ['CLUSTER_TOKEN']}"}
    resp = client.get(f"/worker_poll/{worker_id}", headers=headers)
    assert resp.status_code == 200
    data = resp.get_json()

    # The worker MUST receive the authentic tokens to clone private repos
    assert data["job_id"] == job_id
    assert data["gh_token"] == FAKE_GH_TOKEN
    parsed_env = json.loads(data["env_vars"])
    assert parsed_env["HF_TOKEN"] == FAKE_HF_TOKEN
    assert parsed_env["TOGETHER_API_KEY"] == FAKE_TOGETHER_KEY


def test_scheduler_status_redacts_secrets(client):
    """Verify /scheduler_status does not leak any secret tokens."""
    insert_test_job(status='running')

    resp = client.get("/scheduler_status")
    assert resp.status_code == 200
    text = resp.get_data(as_text=True)

    assert FAKE_GH_TOKEN not in text
    assert FAKE_HF_TOKEN not in text
    assert FAKE_TOGETHER_KEY not in text


def test_api_queue_redacts_secrets(client):
    """Verify /api/queue does not leak secrets."""
    insert_test_job(status='pending')

    with client.session_transaction() as sess:
        sess['user'] = {'login': 'tester'}

    resp = client.get("/api/queue")
    assert resp.status_code == 200
    text = resp.get_data(as_text=True)

    assert FAKE_GH_TOKEN not in text
    assert FAKE_HF_TOKEN not in text
    assert FAKE_TOGETHER_KEY not in text


def test_headnode_logs_redaction(client):
    """Verify /api/jobs/<job_id>/logs redacts token patterns from log streamer responses."""
    job_id = insert_test_job(status='running')

    fake_worker_logs = {
        "logs": (
            f"Step 1: Cloning https://x-access-token:{FAKE_GH_TOKEN}@github.com/UNIL-DESI/test.git\n"
            f"Step 2: Using HF_TOKEN={FAKE_HF_TOKEN} and auth token {FAKE_GH_TOKEN}\n"
        ),
        "offset": 128
    }

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = fake_worker_logs

    with patch('requests.get', return_value=mock_resp):
        resp = client.get(f"/api/jobs/{job_id}/logs")
        assert resp.status_code == 200
        text = resp.get_data(as_text=True)

        assert FAKE_GH_TOKEN not in text
        assert FAKE_HF_TOKEN not in text
        assert "https://****@github.com/UNIL-DESI/test.git" in text


def test_worker_agent_job_logs_redaction(worker_client, tmp_path):
    """Verify worker_agent /job_logs/<job_id> redacts sensitive tokens directly at source."""
    job_id = f"test-worker-{uuid.uuid4().hex[:6]}"
    log_content = (
        f"[INFO] Initializing run with ghp_TESTTESTTEST1234567890\n"
        f"[INFO] Remote: https://x-access-token:ghp_TESTTESTTEST1234567890@github.com/repo.git\n"
    )

    import worker_agent
    orig_logs_dir = worker_agent.LOGS_DIR
    worker_agent.LOGS_DIR = str(tmp_path)

    try:
        log_file = tmp_path / f"{job_id}.log"
        log_file.write_text(log_content, encoding='utf-8')

        resp = worker_client.get(f"/job_logs/{job_id}")
        assert resp.status_code == 200
        text = resp.get_data(as_text=True)

        assert "ghp_TESTTESTTEST1234567890" not in text
        assert "ghp_****" in text
        assert "https://****@github.com/repo.git" in text
    finally:
        worker_agent.LOGS_DIR = orig_logs_dir


def test_api_projects_runs_redaction(client):
    """Verify /api/projects/<path:repo>/runs does not leak secrets."""
    insert_test_job(status='running')

    resp = client.get("/api/projects/UNIL-DESI/test-repo/runs")
    assert resp.status_code == 200
    text = resp.get_data(as_text=True)

    assert FAKE_GH_TOKEN not in text
    assert FAKE_HF_TOKEN not in text
    assert FAKE_TOGETHER_KEY not in text


def test_api_runs_files_error_redaction(client):
    """Verify /api/runs/<job_id>/files redacts errors that might contain token clone URLs."""
    job_id = insert_test_job(status='running')

    with patch('headnode_service.find_local_repo', return_value=None), \
         patch('headnode_service.proxy_request', side_effect=Exception("Worker offline")), \
         patch('subprocess.run') as mock_run:
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr=f"fatal: could not read Username for 'https://x-access-token:{FAKE_GH_TOKEN}@github.com': No such device"
        )

        resp = client.get(f"/api/runs/{job_id}/files")
        assert resp.status_code == 500
        text = resp.get_data(as_text=True)

        assert FAKE_GH_TOKEN not in text
        assert "https://****@github.com" in text
