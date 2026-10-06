"""Local mode remains selected through failures, custom env and old entrypoints."""
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import Mock, patch

import pytest
from src.cluster import cluster_run as client
from src.runner import branch_executor as branch

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('state', [None, 'invalid json', {'is_local': False}, {'is_local': True}])
def test_local_cleanup_never_falls_back_to_git(tmp_path, monkeypatch, state):
    path = tmp_path / 'state.json'
    if state is not None:
        path.write_text(state if isinstance(state, str) else json.dumps(state))
    monkeypatch.setattr(client, 'STATE_FILE', str(path))
    monkeypatch.setattr(client, 'BRANCH', 'local-draft/synthetic')
    monkeypatch.setattr(client, 'RUN_IS_LOCAL', True)
    monkeypatch.setattr(client, '_CLEANUP_DONE', False)
    with patch.object(client, 'fetch_cluster_results') as git, patch.object(client, 'fetch_local_results'):
        client.cleanup()
    git.assert_not_called()


def test_normal_cleanup_still_syncs_git(tmp_path, monkeypatch):
    monkeypatch.setattr(client, 'STATE_FILE', str(tmp_path / 'missing.json'))
    monkeypatch.setattr(client, 'BRANCH', 'cluster-draft/synthetic')
    monkeypatch.setattr(client, 'RUN_IS_LOCAL', False)
    monkeypatch.setattr(client, '_CLEANUP_DONE', False)
    with patch.object(client, 'fetch_cluster_results') as git:
        client.cleanup()
    git.assert_called_once()


@pytest.mark.parametrize('state,branch_name,expected', [
    ({}, 'local-draft/synthetic', True), ({'is_local': False}, 'local-draft/synthetic', True),
    ({}, 'unknown', True), ({'is_local': 'false'}, 'unknown', True),
    ({}, 'cluster-draft/synthetic', False), ({'is_local': False}, 'main', False),
])
def test_orphan_mode_is_conservative(state, branch_name, expected):
    assert client._state_is_local(state, branch_name) is expected


@pytest.mark.parametrize('command', ['sync', 'view', 'list', 'cancel'])
def test_unsupported_local_commands_stop_before_network(command, monkeypatch):
    monkeypatch.setattr(sys, 'argv', ['cluster-run', '--local', command])
    with patch.object(client, 'check_and_redirect_cwd'), patch.object(client.atexit, 'register'), \
            patch.object(client.signal, 'signal'), patch.object(client, 'check_dependencies') as deps, \
            patch.object(client, 'recover_orphaned_run') as recover:
        with pytest.raises(SystemExit) as exc:
            client.main()
    assert exc.value.code == 2
    deps.assert_not_called()
    recover.assert_not_called()


def test_local_mode_selected_before_submission_failure(monkeypatch):
    monkeypatch.setattr(client, 'RUN_IS_LOCAL', None)
    with patch.object(client, 'clean_old_results', side_effect=RuntimeError('synthetic failure')):
        with pytest.raises(RuntimeError):
            client.local_run()
    assert client.RUN_IS_LOCAL is True


@pytest.mark.parametrize('args', [['--local'], ['list', '--local'], ['--local=true'], ['--unknown']])
def test_legacy_launcher_never_publishes_unknown_or_local_options(args):
    source = (ROOT / 'scripts/cluster-run.sh').read_text()
    entry = source[source.index('# --- CLI Entry Point ---'):]
    stubs = 'check_dependencies() { :; }; shadow_run() { echo UNSAFE_PUBLISH_SELECTED; };\n'
    result = subprocess.run(['bash', '-c', stubs + entry, 'fixture', *args], text=True, capture_output=True)
    assert result.returncode == 2
    assert 'UNSAFE_PUBLISH_SELECTED' not in result.stdout


def test_local_pipeline_cannot_enter_external_log_delegation():
    env = dict(os.environ, IS_LOCAL='1', CLUSTER_CI_MODE='delegate')
    result = subprocess.run(['bash', str(ROOT / 'src/runner/run_research_pipeline.sh'), 'lab/test', 'local-draft/test'],
                            env=env, text=True, capture_output=True)
    assert result.returncode == 1
    assert 'Local jobs require executor mode' in result.stderr


def test_installation_env_cannot_downgrade_local_mode(tmp_path):
    script = tmp_path / 'src/runner/run.sh'
    script.parent.mkdir(parents=True)
    source = (ROOT / 'src/runner/run_research_pipeline.sh').read_text()
    prefix = source[:source.index('# Normal workspace and volume names stay unchanged.')]
    script.write_text(prefix + '\nprintf "MODE=%s" "$IS_LOCAL"\n')
    (tmp_path / '.env').write_text('IS_LOCAL=0\n')
    env = dict(os.environ, IS_LOCAL='1', CLUSTER_CI_MODE='executor', CALLER_COMMIT_SHA='synthetic')
    result = subprocess.run(['bash', str(script), 'lab/test', 'local-draft/test'], env=env, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert 'MODE=1' in result.stdout


@pytest.mark.parametrize('local', ['0', '1'])
def test_job_metadata_overrides_stage_secrets(tmp_path, monkeypatch, local):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('IS_LOCAL', local)
    secrets = tmp_path / 'secrets.env'
    secrets.write_text('IS_LOCAL=0\nHEADNODE_URL=https://external.invalid\nJOB_ID=wrong\nFROM_FILE=kept\n')
    monkeypatch.setenv('CLUSTER_CI_SECRETS_FILE', str(secrets))
    docker = Mock(spec=branch.DockerRunner)
    docker.exec_in_container.return_value = (0, '')
    obj = branch.BranchExecutor(headnode_url='http://headnode.invalid', job_id='synthetic',
        runner_id='r', worker_id='w', repo_dir=str(tmp_path), target_repo='lab/test', target_branch='main',
        cluster_token='synthetic-token', docker=docker)
    obj.current_container = 'synthetic'
    with patch.object(branch.subprocess, 'Popen'):
        obj.execute_node_in_container('stage', attempt=2, env_vars={
            'IS_LOCAL': '0', 'CLUSTER_CI_MODE': 'delegate', 'CLUSTER_TOKEN': 'wrong',
            'HEADNODE_URL': 'https://external.invalid', 'CLUSTER_CI_NODE_ATTEMPT': '100', 'USER_PARAMETER': 'kept'})
    env = docker.exec_in_container.call_args.kwargs['env']
    assert env['IS_LOCAL'] == local
    assert env['CLUSTER_CI_MODE'] == 'executor'
    assert env['CLUSTER_TOKEN'] == 'synthetic-token'
    assert env['HEADNODE_URL'] == 'http://headnode.invalid'
    assert env['JOB_ID'] == 'synthetic'
    assert env['CLUSTER_CI_NODE_ATTEMPT'] == '2'
    assert env['USER_PARAMETER'] == env['FROM_FILE'] == 'kept'
