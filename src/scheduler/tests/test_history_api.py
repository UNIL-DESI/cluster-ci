import pytest
import os
os.environ["CLUSTER_TOKEN"] = ""
import json
import sqlite3
from unittest.mock import patch, MagicMock

# Set the DB path before importing anything that might use it
test_db = "test_history.db"
if os.path.exists(test_db):
    os.remove(test_db)
os.environ["CLUSTER_DB_PATH"] = test_db

from headnode_service import app, init_db
from persistence import DB_PATH

@pytest.fixture
def client():
    with app.test_client() as client:
        with app.app_context():
            init_db()
            yield client

    if os.path.exists(test_db):
        os.remove(test_db)

def test_history_apis(client):
    # Mock authentication
    with client.session_transaction() as sess:
        sess['user'] = {'login': 'testuser'}
        sess['token'] = {'access_token': 'fake'}

    # Mock GitHub API for allowed repos
    with patch('headnode_service.oauth.github.get') as mock_get:
        mock_get.return_value.json.return_value = [
            {
                'full_name': 'owner/repo1',
                'owner': {'login': 'UNIL-DESI'},
                'permissions': {'push': True}
            },
            {
                'full_name': 'owner/repo2',
                'owner': {'login': 'UNIL-DESI'},
                'permissions': {'push': True}
            }
        ]

        # Now run the original test logic
        run_history_test_logic(client)

def run_history_test_logic(client):
    # 1. Inject some data
    from persistence import get_db_conn
    with get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO jobs (job_id, repo, branch, commit_hash, status, created_at, ram_required_gb)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', ("job1", "owner/repo1", "main", "hash1", "completed", "2023-01-01 10:00:00", 0))
        cursor.execute('''
            INSERT INTO jobs (job_id, repo, branch, commit_hash, status, created_at, ram_required_gb)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', ("job2", "owner/repo1", "feat", "hash2", "completed", "2023-01-01 11:00:00", 0))
        cursor.execute('''
            INSERT INTO jobs (job_id, repo, branch, commit_hash, status, created_at, ram_required_gb)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', ("job3", "owner/repo2", "main", "hash3", "completed", "2023-01-01 12:00:00", 0))
        conn.commit()

    # 2. Test /api/projects
    resp = client.get('/api/projects')
    assert resp.status_code == 200
    projects = resp.get_json()
    assert "owner/repo1" in projects
    assert "owner/repo2" in projects
    assert len(projects) == 2

    # 3. Test /api/projects/<repo>/runs
    resp = client.get('/api/projects/owner/repo1/runs')
    assert resp.status_code == 200
    runs = resp.get_json()
    assert len(runs) == 2
    assert runs[0]['job_id'] == "job2" # Ordered by created_at DESC
    assert runs[1]['job_id'] == "job1"

    # 4. Test /api/runs/<job_id>/files (with Mocking subprocess)
    with patch('headnode_service.find_local_repo', return_value=None), \
         patch('subprocess.run') as mock_run:
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps([{"path": "data.csv", "isout": True}])
        )

        resp = client.get('/api/runs/job1/files')
        assert resp.status_code == 200
        files = resp.get_json()
        assert len(files) == 1
        assert files[0]['path'] == "data.csv"

        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert "dvc" in args
        assert "list" in args
        assert "https://github.com/owner/repo1.git" in args
        assert "--rev" in args
        assert "hash1" in args

    # 5. Test Error cases
    resp = client.get('/api/runs/nonexistent/files')
    assert resp.status_code == 404

    # Test job without commit_hash
    with get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute('INSERT INTO jobs (job_id, repo, branch, status, ram_required_gb) VALUES (?, ?, ?, ?, ?)', ("job_no_hash", "owner/repo1", "main", "completed", 0))
        conn.commit()

    resp = client.get('/api/runs/job_no_hash/files')
    assert resp.status_code == 400
    assert "Commit hash not found" in resp.get_json()['error']
