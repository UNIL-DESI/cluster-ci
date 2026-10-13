"""Regression coverage for physical Docker IDs vs logical CUDA indices."""
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import Mock, patch

import pytest

from src.runner.host_guard import docker_resource_args
from src.runner.branch_executor import BranchExecutor, DockerRunner


@pytest.mark.parametrize('physical,logical', [([1], '0'), ([0, 1], '0,1'), ([2, 4], '0,1')])
def test_device_flags_preserve_physical_selection_and_remap_cuda(physical, logical):
    flags = docker_resource_args({'role':'worker','total_ram_gb':64},
                                {'gpus':len(physical),'gpu_ids':physical,'ram_gb':4})
    value = next(flag.split('=',1)[1] for flag in flags if flag.startswith('--gpus='))
    # Docker uses a CSV parser for this value. A comma inside the device list
    # must not create a second Count field.
    assert next(csv.reader([value])) == ['device=' + ','.join(map(str, physical))]
    assert '--env=NVIDIA_VISIBLE_DEVICES=' + ','.join(map(str, physical)) in flags
    assert '--env=CUDA_VISIBLE_DEVICES=' + logical in flags
    assert not any(flag.startswith('-e ') for flag in flags)
    with patch('src.runner.branch_executor.subprocess.run', return_value=Mock(returncode=0)) as run:
        DockerRunner().run_container('synthetic','synthetic','synthetic','/synthetic',
                                    '/synthetic',resources={'gpus':len(physical),'gpu_ids':physical,'ram_gb':4})
    assert '--env=CUDA_VISIBLE_DEVICES=' + logical in run.call_args.args[0]


@pytest.mark.parametrize('ids,logical', [('1','0'), ('2,4','0,1'), ('','')])
def test_stage_uses_logical_indices_even_after_custom_environment(tmp_path, monkeypatch, ids, logical):
    monkeypatch.setenv('IS_LOCAL', '1')
    docker = Mock()
    docker.exec_in_container.return_value = (0,'')
    executor = BranchExecutor('', 'synthetic','synthetic','synthetic',str(tmp_path),
                              'lab/synthetic','main',docker=docker)
    executor.current_container = 'synthetic'
    executor.current_pythonpath = '/synthetic'
    with patch('src.runner.branch_executor.subprocess.Popen'):
        executor.execute_node_in_container('stage',gpu_ids_str=ids,
                                          env_vars={'CUDA_VISIBLE_DEVICES':'wrong-host-index'})
    assert docker.exec_in_container.call_args.kwargs['env']['CUDA_VISIBLE_DEVICES'] == logical


def test_classic_multi_gpu_csv_is_one_argv_value():
    root = Path(__file__).resolve().parents[1]
    source = (root/'src/runner/run_research_pipeline.sh').read_text()
    begin = source.index('# GPU flag determination')
    end = source.index('docker run -d', begin)
    script = source[begin:end] + '\nprintf "%s\\n" "${DOCKER_GPU_FLAG[@]}"\n'
    result = subprocess.check_output(['bash','-c',script],
                    env=dict(os.environ,CLUSTER_CI_GPU_IDS='0,1'),text=True).splitlines()
    assert result[0] == '--gpus'
    assert next(csv.reader([result[1]])) == ['device=0,1']
