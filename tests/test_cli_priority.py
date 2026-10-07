"""Tests for CLI --priority submission flag and propagation."""

import argparse
import pytest

from src.scheduler.headnode_service import app
from src.scheduler.persistence import get_db_conn, init_db


@pytest.fixture(autouse=True)
def setup_test_db(tmp_path, monkeypatch):
    test_db = str(tmp_path / "test_cluster_ci.db")
    monkeypatch.setenv("CLUSTER_DB_PATH", test_db)
    init_db()
    yield test_db


def test_submit_job_argparser():
    import src.scheduler.submit_job  # noqa: F401
    parser = argparse.ArgumentParser()
    parser.add_argument("repo", nargs="?", default=None)
    parser.add_argument("branch", nargs="?", default=None)
    parser.add_argument("--priority", choices=["high", "normal", "low"], default=None)

    args = parser.parse_args(["owner/repo", "main", "--priority", "high"])
    assert args.priority == "high"

    args_def = parser.parse_args(["owner/repo", "main"])
    assert args_def.priority is None

    with pytest.raises(SystemExit):
        parser.parse_args(["owner/repo", "main", "--priority", "ultra"])


def test_cluster_run_argparser():
    import src.cluster.cluster_run  # noqa: F401
    parser = argparse.ArgumentParser()
    parser.add_argument("command", nargs="?", default=None, choices=["list", "view", "cancel", "sync", "attach"])
    parser.add_argument("run_id", nargs="?", default=None)
    parser.add_argument("--local", action="store_true")
    parser.add_argument("--priority", choices=["high", "normal", "low"], default=None)

    args = parser.parse_args(["--priority", "low"])
    assert args.priority == "low"

    args_def = parser.parse_args([])
    assert args_def.priority is None

    with pytest.raises(SystemExit):
        parser.parse_args(["--priority", "super"])


def test_headnode_submit_job_priority_validation():
    client = app.test_client()

    # Invalid priority -> 400
    resp_invalid = client.post("/submit_job", json={
        "repo": "owner/repo",
        "branch": "main",
        "priority": "invalid_prio",
    })
    assert resp_invalid.status_code == 400
    assert "Invalid priority" in resp_invalid.get_json()["error"]

    # Valid priority high
    resp_high = client.post("/submit_job", json={
        "repo": "owner/repo",
        "branch": "main",
        "priority": "high",
        "username": "alice",
    })
    assert resp_high.status_code == 200
    job_id = resp_high.get_json()["job_id"]
    assert resp_high.get_json()["scheduling_priority"] == "high"

    with get_db_conn() as conn:
        row = conn.execute("SELECT scheduling_priority FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        assert row is not None
        assert row["scheduling_priority"] == "high"


def test_headnode_submit_job_with_plan_inherits_priority():
    client = app.test_client()
    plan = {
        "version": "3.0",
        "defaults": {"priority": "normal"},
        "nodes": [
            {
                "name": "prepare",
                "deps": [],
                "stale": True,
                "priority": 1.0,
                "scheduling_priority": "normal",
                "resources": {"cpus": 2, "ram_gb": 4, "priority": "normal"},
            },
            {
                "name": "train_custom",
                "deps": ["prepare"],
                "stale": True,
                "priority": 2.0,
                "scheduling_priority": "low",
                "resources": {"cpus": 4, "ram_gb": 8, "priority": "low"},
            }
        ]
    }

    resp = client.post("/submit_job", json={
        "repo": "owner/repo",
        "branch": "main",
        "priority": "high",
        "username": "bob",
        "plan": plan,
    })
    assert resp.status_code == 200
    job_id = resp.get_json()["job_id"]

    with get_db_conn() as conn:
        rows = conn.execute(
            "SELECT node_name, scheduling_priority FROM job_nodes WHERE job_id = ? ORDER BY node_name",
            (job_id,)
        ).fetchall()
        assert len(rows) == 2
        # prepare had normal priority -> inherited high
        prepare_node = next(r for r in rows if r["node_name"] == "prepare")
        assert prepare_node["scheduling_priority"] == "high"
        # train_custom explicitly set low -> remained low
        train_node = next(r for r in rows if r["node_name"] == "train_custom")
        assert train_node["scheduling_priority"] == "low"
