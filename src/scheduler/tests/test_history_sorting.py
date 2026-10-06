import os
os.environ["CLUSTER_TOKEN"] = ""
import pytest
from datetime import datetime, timezone
from unittest.mock import patch

from headnode_service import normalize_iso_utc, app, init_db
from persistence import get_db_conn


def test_normalize_iso_utc_direct():
    # SQLite standard UTC string (no T, no timezone)
    assert normalize_iso_utc("2026-10-06 00:36:00") == "2026-10-06T00:36:00Z"
    
    # ISO 8601 string already in UTC with Z
    assert normalize_iso_utc("2026-10-06T01:16:00Z") == "2026-10-06T01:16:00Z"
    
    # ISO 8601 string with positive timezone offset (e.g. Git %aI in Paris/CEST +02:00)
    # 01:16:00+02:00 converts to 23:16:00Z on previous day
    assert normalize_iso_utc("2026-10-06T01:16:00+02:00") == "2026-10-05T23:16:00Z"

    # Python datetime object (naive, assumed UTC like SQLite)
    dt_naive = datetime(2026, 10, 6, 0, 58, 0)
    assert normalize_iso_utc(dt_naive) == "2026-10-06T00:58:00Z"

    # Python datetime object (aware UTC)
    dt_aware = datetime(2026, 10, 6, 0, 45, 0, tzinfo=timezone.utc)
    assert normalize_iso_utc(dt_aware) == "2026-10-06T00:45:00Z"

    # Empty and None values return None
    assert normalize_iso_utc(None) is None
    assert normalize_iso_utc("") is None
    assert normalize_iso_utc("   ") is None

    # Invalid input raises ValueError (Fail-Fast)
    with pytest.raises(ValueError):
        normalize_iso_utc("invalid-date-string")


def test_mixed_format_same_day_sorting():
    """
    Test sorting on the specific issue #101 scenario:
    Runs created at 00:36, 00:45, 00:58, 01:16 on the same day with mixed
    SQLite ('YYYY-MM-DD HH:MM:SS') and Git ISO-8601 ('YYYY-MM-DDTHH:MM:SSZ') formats.
    """
    raw_runs = [
        {"id": "run_0036", "date": "2026-10-06 00:36:00"},
        {"id": "run_0116", "date": "2026-10-06T01:16:00Z"},
        {"id": "run_0045", "date": "2026-10-06T00:45:00Z"},
        {"id": "run_0058", "date": "2026-10-06 00:58:00"},
    ]

    # Normalize all exposed dates
    normalized_runs = [
        {"id": r["id"], "created_at": normalize_iso_utc(r["date"])}
        for r in raw_runs
    ]

    # Sort newest first (reverse chronological order)
    sorted_runs = sorted(
        normalized_runs,
        key=lambda x: x["created_at"],
        reverse=True
    )

    expected_ids = ["run_0116", "run_0058", "run_0045", "run_0036"]
    actual_ids = [r["id"] for r in sorted_runs]

    assert actual_ids == expected_ids, f"Expected {expected_ids}, got {actual_ids}"
    assert sorted_runs[0]["created_at"] == "2026-10-06T01:16:00Z"
    assert sorted_runs[1]["created_at"] == "2026-10-06T00:58:00Z"
    assert sorted_runs[2]["created_at"] == "2026-10-06T00:45:00Z"
    assert sorted_runs[3]["created_at"] == "2026-10-06T00:36:00Z"


@pytest.fixture
def sorting_client(tmp_path):
    test_db = str(tmp_path / "test_history_sorting.db")
    os.environ["CLUSTER_DB_PATH"] = test_db
    with app.test_client() as client:
        with app.app_context():
            init_db()
            with get_db_conn() as conn:
                try:
                    conn.execute('ALTER TABLE jobs ADD COLUMN local_repo_path TEXT')
                    conn.commit()
                except Exception:
                    pass
            yield client
    if os.path.exists(test_db):
        try:
            os.remove(test_db)
        except Exception:
            pass


def test_api_artifact_history_sorting(sorting_client):
    """
    Test that /api/projects/<repo>/artifact/history returns entries
    with strict ISO UTC dates ordered newest first.
    """
    with sorting_client.session_transaction() as sess:
        sess['user'] = {'login': 'testuser'}

    repo = "UNIL-DESI/test-sorting-repo"
    runs_data = [
        ("job_0036", repo, "main", "hash_0036", "completed", "2026-10-06 00:36:00"),
        ("job_0116", repo, "main", "hash_0116", "completed", "2026-10-06 01:16:00"),
        ("job_0045", repo, "main", "hash_0045", "completed", "2026-10-06 00:45:00"),
        ("job_0058", repo, "main", "hash_0058", "completed", "2026-10-06 00:58:00"),
    ]

    with get_db_conn() as conn:
        cursor = conn.cursor()
        for j_id, r, b, c_hash, st, c_at in runs_data:
            cursor.execute('''
                INSERT INTO jobs (job_id, repo, branch, commit_hash, status, created_at, ram_required_gb)
                VALUES (?, ?, ?, ?, ?, ?, 0)
            ''', (j_id, r, b, c_hash, st, c_at))
        conn.commit()

    with patch('headnode_service.find_local_repo', return_value=None), \
         patch('headnode_service.fetch_dvc_file_distributed', return_value="dummy_content"), \
         patch('headnode_service.parse_dvc_metadata', return_value={"md5": "abc123md5", "size": 1024}):

        resp = sorting_client.get(f'/api/projects/{repo}/artifact/history?path=metrics.json')
        assert resp.status_code == 200
        history = resp.get_json()

        assert len(history) == 4
        # Verify order: 01:16 > 00:58 > 00:45 > 00:36
        ordered_jobs = [h['job_id'] for h in history]
        assert ordered_jobs == ["job_0116", "job_0058", "job_0045", "job_0036"]
        
        # Verify ISO 8601 UTC format on all entries
        for h in history:
            assert h['created_at'].endswith('Z')
            assert 'T' in h['created_at']
