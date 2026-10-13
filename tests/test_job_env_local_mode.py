"""Exercise the shell env-file boundary with synthetic data and no Docker/network."""
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import Mock, patch

import pytest

from src.runner import branch_executor, dvc_git_helper

ROOT = Path(__file__).resolve().parents[1]
CONTROL = {
    'IS_LOCAL': '1',
    'DVC_NO_ANALYTICS': '1',
    'CLUSTER_CI_MODE': 'executor',
    'HEADNODE_URL': 'http://127.0.0.1:5000',
    'JOB_ID': 'synthetic-job',
    'CLUSTER_TOKEN': 'synthetic-token',
}


def run_job_env_block(tmp_path, content, local='1', skip='0'):
    source = (ROOT / 'src/runner/run_research_pipeline.sh').read_text()
    begin = source.index('if [ -n "$CLUSTER_CI_SECRETS_FILE" ]')
    end = source.index('\nfi', begin) + len('\nfi')
    docker_begin = source.index('docker run -d \\\n', end)
    docker_end = source.index('\n\n', docker_begin)
    secrets = tmp_path / 'job.env'
    secrets.write_text(content)
    env_output = tmp_path / 'parent.json'
    docker_output = tmp_path / 'docker.json'
    capture = tmp_path / 'capture.py'
    capture.write_text(
        'import json, os, sys\n'
        'from pathlib import Path\n'
        'if len(sys.argv) > 1:\n'
        '    Path(os.environ["AUDIT_DOCKER"]).write_text(json.dumps(sys.argv[1:]))\n'
        'else:\n'
        f'    keys = {list(CONTROL) + ["SKIP_CODE_INVALIDATION", "USER_PARAMETER"]!r}\n'
        '    Path(os.environ["AUDIT_ENV"]).write_text(json.dumps({k:os.environ.get(k) for k in keys}))\n'
    )
    script = (
        'set -e\nlog_info() { :; }\nlog_error() { echo "$1" >&2; }\n'
        'readonly CLUSTER_CI_SUBMITTED_LOCAL="${IS_LOCAL:-0}"\n'
        + source[begin:end]
        + '\n"$AUDIT_PYTHON" "$AUDIT_CAPTURE"\n'
        + 'docker() { "$AUDIT_PYTHON" "$AUDIT_CAPTURE" "$@"; }\n'
        + source[docker_begin:docker_end] + '\n'
    )
    env = dict(CONTROL, PATH=os.environ['PATH'], IS_LOCAL=local,
               DVC_NO_ANALYTICS='1' if local == '1' else '',
               SKIP_CODE_INVALIDATION=skip, CLUSTER_CI_SECRETS_FILE=str(secrets),
               AUDIT_PYTHON=sys.executable, AUDIT_CAPTURE=str(capture),
               AUDIT_ENV=str(env_output), AUDIT_DOCKER=str(docker_output),
               BASE_DIR=str(tmp_path), MAIN_CONTAINER_NAME='synthetic-container',
               HOME_CACHE_VOLUME='synthetic-volume', DOCKER_IMAGE='synthetic-image')
    result = subprocess.run(['bash', '-c', script], cwd=tmp_path, env=env,
                            text=True, capture_output=True)
    return result, env_output, docker_output, secrets


@pytest.mark.parametrize('local', ['0', '1'])
def test_job_env_cannot_override_control_or_execute_shell(tmp_path, local):
    marker = tmp_path / 'must-not-exist'
    content = (
        'IS_LOCAL=0\nDVC_NO_ANALYTICS=0\nCLUSTER_CI_MODE=delegate\n'
        'HEADNODE_URL=https://example.invalid\nJOB_ID=wrong\nCLUSTER_TOKEN=wrong\n'
        'SKIP_CODE_INVALIDATION=1\nUSER_PARAMETER=some ordinary value=with equals\n'
        f'UNUSED_PARAMETER=$(touch {marker})\n'
    )
    result, env_file, docker_file, secrets = run_job_env_block(tmp_path, content, local)
    assert result.returncode == 0, result.stderr
    parent = json.loads(env_file.read_text())
    expected = dict(CONTROL, IS_LOCAL=local, DVC_NO_ANALYTICS='1' if local == '1' else '')
    assert {key: parent[key] for key in CONTROL} == expected
    assert parent['SKIP_CODE_INVALIDATION'] == '1'
    assert parent['USER_PARAMETER'] is None
    assert not marker.exists()
    args = json.loads(docker_file.read_text())
    assert args[args.index('--env-file') + 1] == str(secrets)
    assert secrets.read_text() == content  # ordinary job parameters are retained
    explicit = dict(args[i + 1].split('=', 1) for i, arg in enumerate(args) if arg == '-e')
    assert {key: explicit[key] for key in CONTROL} == expected

    # Actual publishing guard sees the shell's preserved mode.
    with patch.dict(os.environ, expected, clear=True), \
         patch.object(dvc_git_helper, 'install_dvc_lock_merge_driver'), \
         patch.object(dvc_git_helper, '_get_git_env', return_value={}), \
         patch.object(dvc_git_helper.subprocess, 'run', return_value=Mock(returncode=0)) as run:
        dvc_git_helper.push_with_retries(current_branch='synthetic', cwd=str(tmp_path))
    assert any(c.args[0][:2] == ['git', 'push'] for c in run.call_args_list) == (local == '0')
    if local == '1':
        with patch.dict(os.environ, expected, clear=True), \
             patch.object(branch_executor, 'dvc_git_helper') as helper:
            assert branch_executor.commit_and_push_node(str(tmp_path), 'stage', 'branch')
        helper.push_with_retries.assert_not_called()


def test_local_metrics_keep_cluster_destination(tmp_path, monkeypatch):
    result, env_file, _, _ = run_job_env_block(tmp_path, 'HEADNODE_URL=https://example.invalid\n')
    assert result.returncode == 0, result.stderr
    parent = {k: v for k, v in json.loads(env_file.read_text()).items() if v is not None}
    monkeypatch.chdir(tmp_path)
    (tmp_path / 'metrics.json').write_text('{"synthetic":1}')
    with patch.dict(os.environ, parent, clear=True), \
         patch.object(dvc_git_helper, 'get_cache_false_paths', return_value=['metrics.json']), \
         patch.object(dvc_git_helper, 'cluster_urlopen') as transport:
        response = transport.return_value.__enter__.return_value
        response.status = 200
        response.read.return_value = b'{"synced_files":1}'
        dvc_git_helper._sync_metrics_http()
    request = transport.call_args.args[0]
    assert request.full_url == CONTROL['HEADNODE_URL'] + '/api/jobs/synthetic-job/sync_results'
    assert b'{"synthetic":1}' in request.data


@pytest.mark.parametrize('content,initial,expected', [
    ('', '1', '1'), ('OTHER=value\n', '0', '0'),
    ('SKIP_CODE_INVALIDATION=1\n', '0', '1'),
    ('SKIP_CODE_INVALIDATION=0\n', '1', '0'),
    ('SKIP_CODE_INVALIDATION=1', '0', '1'),
])
def test_skip_code_switch_preserved(tmp_path, content, initial, expected):
    result, env_file, _, _ = run_job_env_block(tmp_path, content, skip=initial)
    assert result.returncode == 0, result.stderr
    assert json.loads(env_file.read_text())['SKIP_CODE_INVALIDATION'] == expected


def test_invalid_host_setting_stops_without_shell_execution(tmp_path):
    marker = tmp_path / 'must-not-exist'
    result, env_file, docker_file, _ = run_job_env_block(
        tmp_path, f'SKIP_CODE_INVALIDATION=$(touch {marker})\n')
    assert result.returncode != 0
    assert 'SKIP_CODE_INVALIDATION must be 0 or 1' in result.stderr
    assert not marker.exists()
    assert not env_file.exists()
    assert not docker_file.exists()
