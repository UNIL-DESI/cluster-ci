"""A failed exec must not leave descendants alive after its reservation releases."""
from types import SimpleNamespace
import pytest
from src.runner import branch_executor as branch


@pytest.mark.parametrize('local', ['0', '1'])
def test_failed_container_stops_before_reporting_or_retry(tmp_path, monkeypatch, local):
    monkeypatch.setenv('IS_LOCAL', local)
    from src.runner import local_workspace
    monkeypatch.setattr(local_workspace, 'preserve_pending_runners', lambda *a, **k: None)
    monkeypatch.setattr(local_workspace, 'restore_remote_node', lambda *a, **k: None)
    executor = branch.BranchExecutor('', 'job', 'runner', 'worker', str(tmp_path),
                                   'test/repo', 'main', start_commit='HEAD')
    monkeypatch.setattr(executor, '_start_heartbeat', lambda: None)
    monkeypatch.setattr(executor, '_stop_heartbeat', lambda: None)
    monkeypatch.setattr(branch, 'sync_before_node', lambda **kw: None)
    from src.runner import workspace_sanitizer
    monkeypatch.setattr(workspace_sanitizer, 'sanitize_workspace', lambda *a: None)
    monkeypatch.setattr(executor, 'fetch_missing_deps', lambda **kw: [])
    monkeypatch.setattr(executor, 'compute_docker_resource_args', lambda r: [])
    executor.docker = SimpleNamespace(is_oom_killed=lambda c: False)
    alive = []; events = []; calls = []

    def start(image, resources):
        assert not alive, 'Prior failure left a live descendant'
        executor.current_container = 'test-container'
        executor.current_image = image
        executor.current_resource_args = []
        alive.append('background descendant')
        events.append('start')

    def stop():
        if executor.current_container:
            alive.clear();events.append('stop');executor.current_container=None

    def archive(*args, **kw):
        assert not alive, 'Private outputs archived before physical stop'

    monkeypatch.setattr(local_workspace, 'preserve_runner_record', archive)

    def next_node(**kw):
        calls.append(kw)
        if len(calls)>1:
            assert not alive, 'Reservation released while descendant was still live'
            assert kw['status']=='failed' and kw['exit_code']==7
        return dict(action='run',node='stage',image='synthetic',resources={}) if len(calls)<3 else dict(action='finish')

    monkeypatch.setattr(executor, 'start_container_for_image', start)
    monkeypatch.setattr(executor, 'stop_current_container', stop)
    monkeypatch.setattr(executor, 'call_next_node', next_node)
    monkeypatch.setattr(executor, 'execute_node_in_container', lambda **kw: (7, 'Synthetic failure'))
    assert executor.run()==0
    assert events==['start','stop','start','stop']
