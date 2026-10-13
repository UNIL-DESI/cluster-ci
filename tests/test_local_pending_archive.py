import io
from unittest.mock import patch
import zipfile

import pytest

from test_local_parallel_handoff import env, run_node, AUTH


def test_failed_upload_does_not_replace_new_checkpoint_with_old_headnode_copy(env):
    assert run_node(env, 'worker-one', 'checkpoint', fail=True)['status'] == 'failed'
    with patch('src.runner.local_workspace.upload_node_archive', side_effect=ConnectionError('synthetic upload failure')):
        with pytest.raises(ConnectionError):
            run_node(env, 'worker-one', 'checkpoint', fail=True)
    assert run_node(env, 'worker-one', 'checkpoint')['status'] == 'done'
    response = env['client'].get(f"/api/jobs/{env['job']}/node_results/checkpoint", headers=AUTH)
    with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
        assert archive.read('outputs/checkpoint.txt') == b'3'
