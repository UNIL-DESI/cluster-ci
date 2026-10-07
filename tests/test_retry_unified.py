import os
import sqlite3
import pytest
from src.scheduler.persistence import (
    handle_node_failure_or_retry,
    is_non_retryable_failure,
    init_db
)

@pytest.fixture
def test_db(tmp_path):
    db_file = tmp_path / "test_cluster_ci.db"
    orig_db = os.environ.get("CLUSTER_DB_PATH")
    os.environ["CLUSTER_DB_PATH"] = str(db_file)
    init_db()
    conn = sqlite3.connect(str(db_file))
    conn.row_factory = sqlite3.Row
    yield conn, str(db_file)
    conn.close()
    if orig_db is not None:
        os.environ["CLUSTER_DB_PATH"] = orig_db
    else:
        os.environ.pop("CLUSTER_DB_PATH", None)

def setup_node(conn, job_id="job-1", node_name="stage1", status="running", retry_count=0):
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR IGNORE INTO jobs (job_id, status)
        VALUES (?, 'running')
    """, (job_id,))
    cursor.execute("""
        INSERT INTO job_nodes (job_id, node_name, status, retry_count, worker_id, runner_id)
        VALUES (?, ?, ?, ?, 'worker-1', 'runner-1')
    """, (job_id, node_name, status, retry_count))
    conn.commit()

def test_is_non_retryable_failure():
    is_fatal, reason = is_non_retryable_failure("HostMemoryPressureExceeded", None)
    assert is_fatal is True
    assert reason == "HostMemoryPressureExceeded"

    is_fatal, reason = is_non_retryable_failure(None, "Fail-fast package verification failed: pkg missing")
    assert is_fatal is True
    assert reason == "PackageVerificationFailed"

    is_fatal, reason = is_non_retryable_failure("other_error", "standard crash")
    assert is_fatal is False
    assert reason is None

def test_handle_node_failure_or_retry_preemption(test_db):
    conn, _ = test_db
    cursor = conn.cursor()
    try:
        cursor.execute("ALTER TABLE job_nodes ADD COLUMN preempt_count INTEGER DEFAULT 0")
        cursor.execute("ALTER TABLE job_nodes ADD COLUMN preempted_at TIMESTAMP")
        cursor.execute("ALTER TABLE job_nodes ADD COLUMN preempted_by TEXT")
        conn.commit()
    except sqlite3.OperationalError:
        pass
    setup_node(conn, "job-p", "node-p", status="running", retry_count=1)

    res = handle_node_failure_or_retry(
        conn,
        job_id="job-p",
        node_name="node-p",
        is_preempted=True,
        preempted_by="job-high",
        duration_s=15.0
    )

    assert res["action"] == "preempted"
    assert res["status"] == "ready"

    cursor = conn.cursor()
    cursor.execute("SELECT status, retry_count, preempt_count, preempted_by, worker_id FROM job_nodes WHERE job_id='job-p'")
    row = cursor.fetchone()
    assert row["status"] == "ready"
    assert row["retry_count"] == 1  # Unchanged!
    assert row["preempt_count"] == 1  # Incremented!
    assert row["preempted_by"] == "job-high"
    assert row["worker_id"] is None

def test_handle_node_failure_or_retry_normal_retry(test_db):
    conn, _ = test_db
    setup_node(conn, "job-r", "node-r", status="running", retry_count=0)

    res = handle_node_failure_or_retry(
        conn,
        job_id="job-r",
        node_name="node-r",
        failure_reason="transient_network_error",
        error_message="Connection timed out",
        exit_code=1,
        max_retries=2
    )

    assert res["action"] == "retry"
    assert res["status"] == "ready"
    assert res["retry_count"] == 1

    cursor = conn.cursor()
    cursor.execute("SELECT status, retry_count, failure_reason, worker_id FROM job_nodes WHERE job_id='job-r'")
    row = cursor.fetchone()
    assert row["status"] == "ready"
    assert row["retry_count"] == 1
    assert row["failure_reason"] == "transient_network_error"
    assert row["worker_id"] is None

def test_handle_node_failure_or_retry_exhausted(test_db):
    conn, _ = test_db
    setup_node(conn, "job-e", "node-e", status="running", retry_count=2)

    res = handle_node_failure_or_retry(
        conn,
        job_id="job-e",
        node_name="node-e",
        failure_reason=None,
        exit_code=1,
        max_retries=2
    )

    assert res["action"] == "failed"
    assert res["status"] == "failed"
    assert res["failure_reason"] == "retries_exhausted"

    cursor = conn.cursor()
    cursor.execute("SELECT status, retry_count, failure_reason FROM job_nodes WHERE job_id='job-e'")
    row = cursor.fetchone()
    assert row["status"] == "failed"
    assert row["retry_count"] == 2
    assert row["failure_reason"] == "retries_exhausted"

def test_handle_node_failure_or_retry_fatal_error(test_db):
    conn, _ = test_db
    setup_node(conn, "job-f", "node-f", status="running", retry_count=0)

    res = handle_node_failure_or_retry(
        conn,
        job_id="job-f",
        node_name="node-f",
        error_message="HostMemoryPressureExceeded on worker",
        max_retries=2
    )

    assert res["action"] == "failed"
    assert res["status"] == "failed"
    assert res["failure_reason"] == "HostMemoryPressureExceeded"

    cursor = conn.cursor()
    cursor.execute("SELECT status, retry_count, failure_reason FROM job_nodes WHERE job_id='job-f'")
    row = cursor.fetchone()
    assert row["status"] == "failed"
    assert row["retry_count"] == 0
    assert row["failure_reason"] == "HostMemoryPressureExceeded"
