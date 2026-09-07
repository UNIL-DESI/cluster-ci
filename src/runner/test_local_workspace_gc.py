"""Same eviction policy, separate paths, and no local-workspace uploads."""
import json
import os
import subprocess
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from src.runner import gc_orchestrator as gc


@pytest.mark.parametrize('mode,key,bind', [('', 'lab/project', '0.0.0.0'), ('1', '_local/lab/project', '127.0.0.1')])
def test_runner_namespace(mode, key, bind):
    source = Path(__file__).with_name('run_research_pipeline.sh').read_text()
    block = source[source.index('WORKSPACE_KEY='):source.index('REPO_WORK_DIR=')]
    response = subprocess.run(['bash', '-c', block + '\nprintf "%s\\n%s\\n" "$WORKSPACE_KEY" "$VIEWER_BIND_ADDRESS"'], env={**os.environ, 'TARGET_REPO': 'lab/project', 'IS_LOCAL': mode}, text=True, capture_output=True, check=True)
    assert response.stdout.splitlines() == [key, bind]


@pytest.mark.parametrize('emergency', [False, True])
def test_gc_evicts_oldest_idle_workspace_in_either_mode_without_local_push(tmp_path, monkeypatch, emergency):
    monkeypatch.setattr(gc, 'get_repositories_dir', lambda: tmp_path)
    monkeypatch.setenv('HEADNODE_URL', 'http://test-headnode')
    registry = {}
    for name, when, status in [('_local/lab/project', 1, 'idle'), ('lab/project', 2, 'idle'), ('_local/lab/running', 0, 'running')]:
        path = tmp_path / name
        (path / '.dvc').mkdir(parents=True)
        (path / '.dvc/config').write_text('[core]\nremote = backup\n')
        registry[name] = {'status': status, 'last_execution': when}
    (tmp_path / 'registry.json').write_text(json.dumps(registry))
    evicted = []
    def remove(path, name):
        evicted.append(name)
    with patch.object(gc, 'get_free_space', return_value=0), patch.object(gc, 'cleanup_level_5', side_effect=remove), patch.object(gc.subprocess, 'run', return_value=Mock(returncode=0)) as run, patch.object(gc.requests, 'get', return_value=Mock(status_code=200, json=lambda: {'sufficient': True})):
        (gc.run_gc if emergency else gc.run_transfer_gc)()
    assert evicted == ['_local/lab/project', 'lab/project']
    pushes = [call for call in run.call_args_list if 'push' in call.args[0]]
    assert len(pushes) == (0 if emergency else 1)
    if pushes:
        assert pushes[0].kwargs['cwd'] == tmp_path / 'lab/project'
    saved = json.loads((tmp_path / 'registry.json').read_text())
    assert saved['_local/lab/project']['status'] == 'deleted'
    assert saved['_local/lab/running']['status'] == 'running'


def test_gc_registry_keeps_normal_and_local_lifecycles_independent(tmp_path, monkeypatch):
    monkeypatch.setattr(gc, 'get_repositories_dir', lambda: tmp_path)
    normal, local = 'lab/project', '_local/lab/project'
    gc.update_running(normal)
    gc.update_running(local)
    gc.update_idle(local, str(tmp_path / local))
    registry = json.loads((tmp_path / 'registry.json').read_text())
    assert registry[normal]['status'] == 'running'
    assert registry[local]['status'] == 'idle'
    with patch.object(gc.subprocess, 'run') as run:
        gc.cleanup_level_3(tmp_path / normal, normal)
        gc.cleanup_level_3(tmp_path / local, local)
    assert run.call_args_list[0].args[0][-1] == 'cluster-ci-home-lab-project'
    assert run.call_args_list[1].args[0][-1] == 'cluster-ci-home-_local-lab-project'
