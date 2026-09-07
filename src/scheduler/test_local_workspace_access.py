"""Local file access tests use Flask clients and synthetic files, never a cluster."""
import io
import json
import os
import sys
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault('HEADNODE_URL', 'http://127.0.0.1:1')
with patch('threading.Thread.start'):
    import headnode_service as headnode
    import worker_agent as worker

TOKEN = 'synthetic-test-token'
AUTH = {'Authorization': 'Bearer ' + TOKEN}


@pytest.fixture
def clients(tmp_path, monkeypatch):
    monkeypatch.setattr(worker, 'REPOS_DIR', str(tmp_path))
    monkeypatch.setattr(headnode, 'REPOS_DIR', str(tmp_path))
    monkeypatch.setattr(worker, 'CLUSTER_TOKEN', TOKEN)
    monkeypatch.setattr(headnode, 'CLUSTER_TOKEN', TOKEN)
    monkeypatch.setattr(headnode.app, 'secret_key', 'test-session-signing-key')
    normal = tmp_path / 'lab/project'
    local = tmp_path / '_local/lab/project'
    for root, content in [(normal, 'normal'), (local, 'local')]:
        root.mkdir(parents=True)
        (root / 'result.txt').write_text(content)
    return worker.app.test_client(), headnode.app.test_client(), normal, local


@pytest.mark.parametrize('path,method', [
    ('/fetch_artifact/_local/lab/project/result.txt', 'GET'),
    ('/api/worker/dvc/get?repo=lab/project&local=1&path=result.txt', 'GET'),
    ('/api/worker/dvc/list?repo=lab/project&local=1', 'GET'),
    ('/api/worker/dvc-viewer/start?local=1', 'POST'),
    ('/api/worker/local/view/lab/project/', 'GET'),
    ('/fetch_artifact/_local_results/example.zip', 'GET'),
    ('/fetch_artifact/_local_uploads/example.tar.gz', 'GET'),
    ('/fetch_artifact/_local_transfers/example/chunk', 'GET'),
])
@pytest.mark.parametrize('token', [None, '', 'Bearer wrong', 'Basic synthetic-test-token'])
def test_local_routes_reject_without_token(clients, path, method, token):
    client, _, _, _ = clients
    headers = {'Authorization': token} if token is not None else {}
    with patch.object(worker.subprocess, 'run') as run:
        response = client.open(path, method=method, headers=headers, json={'repo': 'lab/project'})
    assert response.status_code == 401
    run.assert_not_called()


def test_unset_server_token_fails_closed(clients, monkeypatch):
    client, browser, _, _ = clients
    monkeypatch.setattr(worker, 'CLUSTER_TOKEN', None)
    monkeypatch.setattr(headnode, 'CLUSTER_TOKEN', None)
    assert client.get('/fetch_artifact/_local/lab/project/result.txt', headers=AUTH).status_code == 401
    assert browser.get('/api/jobs/test/results', headers=AUTH).status_code == 401


def test_normal_and_local_files_are_separate(clients):
    client, _, _, _ = clients
    assert client.get('/fetch_artifact/lab/project/result.txt').data == b'normal'
    response = client.get('/api/worker/dvc/get?repo=lab/project&local=1&path=result.txt', headers=AUTH)
    assert response.status_code == 200
    assert response.data == b'local'
    with patch.object(worker.subprocess, 'run', return_value=Mock(returncode=1, stdout=b'')):
        response = client.get('/api/worker/dvc/get?repo=lab/project&path=result.txt')
    assert response.status_code == 200
    assert response.data == b'normal'


def test_local_file_read_never_runs_git_or_dvc(clients):
    client, _, _, _ = clients
    with patch.object(worker.subprocess, 'run', side_effect=AssertionError('unexpected subprocess')):
        response = client.get('/api/worker/dvc/get?repo=lab/project&local=1&rev=local-old&path=result.txt', headers=AUTH)
    assert response.data == b'local'


def test_normal_repository_alias_cannot_resolve_local_workspace(clients):
    client, _, normal, local = clients
    (normal.parent / 'alias').symlink_to(local, target_is_directory=True)
    assert client.get('/api/worker/dvc/get?repo=lab/alias&path=result.txt').status_code == 400
    assert client.get('/fetch_artifact/lab/alias/result.txt').status_code == 401
    assert headnode.find_local_repo('lab/alias', auto_clone=False) is None


def test_listing_contains_metadata_only(clients):
    client, _, _, _ = clients
    response = client.get('/api/worker/dvc/list?repo=lab/project&local=1', headers=AUTH)
    assert response.status_code == 200
    assert response.json[0]['path'] == 'result.txt'
    assert 'content' not in response.json[0]


def test_local_result_archive_still_requires_token(clients):
    _, browser, _, _ = clients
    assert browser.get('/api/jobs/00000000-0000-0000-0000-000000000001/results').status_code == 401
    assert browser.get('/api/jobs/00000000-0000-0000-0000-000000000001/results', headers=AUTH).status_code == 404


def test_dashboard_is_not_locked(clients):
    _, browser, _, _ = clients
    with browser.session_transaction() as session:
        session['user'] = {'login': 'lab-member', 'avatar_url': ''}
    response = browser.get('/')
    assert response.status_code == 200
    assert b'Enter the cluster token' not in response.data


def test_local_artifact_requires_token_but_normal_keeps_handler(clients):
    _, browser, _, _ = clients
    with patch.object(headnode, 'local_worker_get', return_value='local result') as local_get:
        assert browser.get('/artifacts/lab/project/local-test/result.txt').status_code == 401
        local_get.assert_not_called()
        response = browser.get('/artifacts/lab/project/local-test/result.txt', headers=AUTH)
        assert response.data == b'local result'
    with patch.object(headnode, 'is_local_revision', return_value=False), patch.object(headnode, 'get_db_conn') as conn, patch.object(headnode, 'proxy_request', return_value=headnode.app.response_class('normal')):
        conn.return_value.__enter__.return_value.cursor.return_value.fetchall.return_value = [{'service_url': 'http://worker'}]
        assert browser.get('/artifacts/lab/project/main/result.txt').data == b'normal'


def test_browser_unlock_is_local_only_and_rotation_invalidates_it(clients, monkeypatch):
    _, browser, _, _ = clients
    with browser.session_transaction() as session:
        session['user'] = {'login': 'lab-member'}
    assert browser.post('/local-access', data={'cluster_token': 'wrong'}).status_code == 401
    response = browser.post('/local-access', data={'cluster_token': TOKEN})
    assert response.status_code == 302
    with browser.session_transaction() as session:
        assert TOKEN not in json.dumps(dict(session))
    with patch.object(headnode, 'local_worker_get', return_value='local result'):
        assert browser.get('/artifacts/lab/project/local-test/result.txt').status_code == 200
        # Browser unlock does not authorize write/transfer API operations.
        assert browser.get('/api/jobs/00000000-0000-0000-0000-000000000001/results').status_code == 401
        monkeypatch.setattr(headnode, 'CLUSTER_TOKEN', 'rotated-token')
        assert browser.get('/artifacts/lab/project/local-test/result.txt').status_code == 401


def test_local_metadata_fetch_uses_protected_namespace_without_git_fallback(clients):
    with patch.object(headnode, 'local_worker', return_value='http://worker'), patch.object(headnode.requests, 'get', return_value=Mock(status_code=200, text='stages: {}')) as get, patch.object(headnode, 'find_local_repo', side_effect=AssertionError('normal namespace used')):
        text = headnode.fetch_dvc_file_distributed('lab/project', 'local-test', 'dvc.yaml', {'is_local': 1})
    assert text == 'stages: {}'
    assert get.call_args.kwargs['params']['local'] == '1'
    assert get.call_args.kwargs['headers'] == AUTH
    assert get.call_args.kwargs['allow_redirects'] is False


def test_local_proxy_does_not_forward_token_or_browser_cookie_to_viewer(clients):
    client, _, _, local = clients
    (local / '.cluster-ci-viewer-port').write_text('12345')
    response = Mock(status_code=200, headers={'Content-Type': 'text/plain'})
    response.iter_content.return_value = iter([b'viewer response'])
    with patch.object(worker.requests, 'request', return_value=response) as request:
        result = client.get('/api/worker/local/view/lab/project/', headers={**AUTH, 'Cookie': 'secret=session'})
        assert result.data == b'viewer response'
    assert request.call_args.args[1].startswith('http://127.0.0.1:12345/')
    assert 'Authorization' not in request.call_args.kwargs['headers']
    assert 'cookies' not in request.call_args.kwargs
    response.close.assert_called_once()


def test_local_viewer_start_uses_workspace_and_loopback_without_git(clients):
    client, _, _, local = clients
    process = Mock()
    process.poll.return_value = None
    with patch.object(worker, 'get_free_port', return_value=12345), patch.object(worker, 'get_executable', return_value='dvc-viewer'), patch.object(worker, 'prepare_dvc_worktree') as prepare, patch.object(worker.subprocess, 'Popen', return_value=process) as start, patch.object(worker.socket, 'socket'):
        response = client.post('/api/worker/dvc-viewer/start?local=1', headers=AUTH, json={'repo': 'lab/project'})
    assert response.status_code == 200
    prepare.assert_not_called()
    assert start.call_args.kwargs['cwd'] == str(local)
    assert start.call_args.args[0][-2:] == ['--host', '127.0.0.1']
    assert (local / '.cluster-ci-viewer-port').read_text() == '12345'


def test_local_viewer_start_failure_does_not_remove_workspace(clients):
    client, _, _, local = clients
    with patch.object(worker.subprocess, 'Popen', side_effect=OSError('synthetic failure')), patch.object(worker, 'safe_cleanup_worktree') as cleanup:
        response = client.post('/api/worker/dvc-viewer/start?local=1', headers=AUTH, json={'repo': 'lab/project'})
    assert response.status_code == 500
    cleanup.assert_not_called()
    assert (local / 'result.txt').read_text() == 'local'


def test_headnode_local_listing_remains_available_without_browser_unlock(clients):
    _, browser, _, _ = clients
    with patch.object(headnode, 'get_db_conn') as conn, patch.object(headnode, 'local_worker_get', return_value=headnode.app.response_class('[{"path":"result.txt"}]', mimetype='application/json')) as get:
        conn.return_value.__enter__.return_value.cursor.return_value.fetchone.return_value = {'repo': 'lab/project', 'is_local': 1}
        response = browser.get('/api/runs/example/files')
    assert response.status_code == 200
    assert response.json == [{'path': 'result.txt'}]
    get.assert_called_once_with('lab/project', '/api/worker/dvc/list', path='')


def test_public_local_status_and_logs_remain_available(clients):
    _, browser, _, _ = clients
    with patch.object(headnode, 'get_db_conn') as conn:
        conn.return_value.__enter__.return_value.cursor.return_value.fetchone.return_value = {'job_id': 'example', 'is_local': 1, 'status': 'running', 'started_at': '2026-01-01', 'worker_service_url': 'http://worker', 'service_url': 'http://worker'}
        response = browser.get('/job_status/example')
        assert response.status_code == 200
        assert response.json['status'] == 'running'
        with patch.object(headnode.requests, 'get', return_value=Mock(status_code=200, json=lambda: {'logs': 'synthetic log', 'offset': 13})):
            response = browser.get('/api/jobs/example/logs')
    assert response.status_code == 200
    assert response.json['logs'] == 'synthetic log'


def test_headnode_injects_token_only_on_local_proxy(clients):
    _, browser, _, _ = clients
    remote = Mock(status_code=200, headers={'Content-Type': 'application/octet-stream'})
    remote.raw.headers = {'Content-Type': 'application/octet-stream'}
    remote.iter_content.return_value = iter([b'local'])
    with headnode.app.test_request_context('/artifacts/lab/project/local-test/result.txt'), patch.object(headnode.requests, 'request', return_value=remote) as request:
        response = headnode.proxy_request('http://worker/api/worker/dvc/get?local=1', local_worker_api=True)
        assert response.status_code == 200
    assert request.call_args.kwargs['headers']['Authorization'] == 'Bearer ' + TOKEN
    assert request.call_args.kwargs['allow_redirects'] is False
    assert request.call_args.kwargs['cookies'] == {}


def test_local_token_not_accepted_as_query_parameter(clients):
    client, _, _, _ = clients
    response = client.get('/fetch_artifact/_local/lab/project/result.txt?token=' + TOKEN)
    assert response.status_code == 401


def test_local_viewer_assets_stay_in_protected_route_without_query_flags(clients):
    _, browser, _, _ = clients
    with browser.session_transaction() as session:
        session['user'] = {'login': 'lab-member'}
    with patch.object(headnode, 'get_db_conn') as conn:
        conn.return_value.__enter__.return_value.execute.return_value.fetchone.return_value = None
        assert browser.get('/local-view/lab/project/app.js').status_code == 401
        browser.post('/local-access', data={'cluster_token': TOKEN})
        with patch.object(headnode, 'local_worker', return_value='http://worker'), patch.object(headnode, 'proxy_request', return_value='asset') as proxy:
            assert browser.get('/local-view/lab/project/app.js').data == b'asset'
            assert proxy.call_args.kwargs['local_worker_api'] is True


def test_normal_viewer_keeps_existing_public_bind_address(clients):
    client, _, normal, _ = clients
    process = Mock()
    process.poll.return_value = None
    with patch.object(worker, 'get_free_port', return_value=12345), patch.object(worker, 'get_executable', return_value='dvc-viewer'), patch.object(worker, 'prepare_dvc_worktree') as prepare, patch.object(worker.subprocess, 'Popen', return_value=process) as start, patch.object(worker.socket, 'socket'):
        response = client.post('/api/worker/dvc-viewer/start', json={'repo': 'lab/project'})
    assert response.status_code == 200
    prepare.assert_called_once()
    assert start.call_args.args[0][-2:] == ['--host', '0.0.0.0']
    assert start.call_args.kwargs['cwd'].startswith('/tmp/dvc-viewer-lab-project-')


def test_pending_sync_drain_skips_local_workspaces(clients, tmp_path, monkeypatch):
    root = tmp_path / 'installation'
    (root / 'repositories').mkdir(parents=True)
    (root / 'repositories/registry.json').write_text(json.dumps({'_local/lab/project': {'sync_status': 'pending'}}))
    monkeypatch.setattr(worker, '__file__', str(root / 'src/scheduler/worker_agent.py'))
    with patch.object(worker.requests, 'get') as get, patch.object(worker.subprocess, 'run') as run:
        worker.drain_pending_syncs()
    get.assert_not_called()
    run.assert_not_called()
