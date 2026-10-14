"""Exercise actual Docker flags for trusted worker hardware metadata."""
import ast,json,os,subprocess
from pathlib import Path
from unittest.mock import Mock,patch
import pytest
from src.runner.branch_executor import BranchExecutor,DockerRunner
ROOT=Path(__file__).resolve().parents[1]
GB10=dict(role='worker',is_headnode=False,unified_memory=True,total_ram_gb=121.63,cpus=20,disk_free_gb=1000,gpu_name='NVIDIA GB10',arch='aarch64')
DISCRETE=dict(role='worker',is_headnode=False,unified_memory=False,total_ram_gb=125,cpus=24,disk_free_gb=1000,gpu_name='RTX 3090',arch='x86_64')

@pytest.mark.parametrize('profile,expected',[(GB10,'110g'),(DISCRETE,'10g')])
def test_docker_launch_and_reuse_share_actual_host_limits(tmp_path,monkeypatch,profile,expected):
 monkeypatch.setenv('CLUSTER_CI_HOST_PROFILE',json.dumps(profile))
 resources=dict(ram_gb=10,vram_gb=100,gpus=1,gpu_ids=[0],cpus=8)
 with patch('src.runner.branch_executor.subprocess.run',return_value=Mock(returncode=0)) as run:
  DockerRunner().run_container('synthetic','synthetic','synthetic',str(tmp_path),str(tmp_path),resources=resources)
 flags=run.call_args.args[0]
 assert '--memory='+expected in flags
 assert '--memory-swap='+expected in flags
 assert '--oom-score-adj=500' in flags and '--pids-limit=4096' in flags
 executor=BranchExecutor('','synthetic','synthetic','synthetic',str(tmp_path),'lab/synthetic','main',docker=Mock())
 assert '--memory='+expected in executor.compute_docker_resource_args(resources)


def test_worker_profile_uses_raw_capacities_not_reserved_advertisements():
 source=ast.parse((ROOT/'src/scheduler/worker_agent.py').read_text())
 node=next(n for n in source.body if isinstance(n,ast.FunctionDef) and n.name=='get_runner_host_profile')
 ns=dict(ROLE='headnode',get_ram_info=lambda:(125.8,100),get_storage_info=lambda:(2000,1000),
         get_gpu_info=lambda:('RTX 3090',48,2,40,[24,24],0),get_cpu_info=lambda:24,get_arch_info=lambda:'x86_64')
 exec(compile(ast.fix_missing_locations(ast.Module(body=[node],type_ignores=[])),'<worker-profile>','exec'),ns)
 p=ns['get_runner_host_profile']()
 assert p['total_ram_gb']==125.8 and p['cpus']==24 and p['is_headnode']
 # Execute the actual injection into the worker-owned launcher environment.
 execute=next(n for n in source.body if isinstance(n,ast.FunctionDef) and n.name=='execute_job')
 assignment=next(n for n in ast.walk(execute) if isinstance(n,ast.Assign) and any(
   isinstance(t,ast.Subscript) and isinstance(t.value,ast.Name) and t.value.id=='env'
   and isinstance(t.slice,ast.Constant) and t.slice.value=='CLUSTER_CI_HOST_PROFILE' for t in n.targets))
 ns.update(env={},json=json)
 exec(compile(ast.fix_missing_locations(ast.Module(body=[assignment],type_ignores=[])),'<worker-injection>','exec'),ns)
 assert json.loads(ns['env']['CLUSTER_CI_HOST_PROFILE'])==p


def test_job_secret_file_cannot_replace_parent_hardware_profile(tmp_path):
 source=(ROOT/'src/runner/run_research_pipeline.sh').read_text()
 begin=source.index('if [ -n "$CLUSTER_CI_SECRETS_FILE" ]')
 end=source.index('\nfi',begin)+len('\nfi')
 secrets=tmp_path/'job.env';secrets.write_text('CLUSTER_CI_HOST_PROFILE={"unified_memory":false}\nIS_LOCAL=0\n')
 env=dict(os.environ,IS_LOCAL='1',CLUSTER_CI_SECRETS_FILE=str(secrets),CLUSTER_CI_HOST_PROFILE=json.dumps(GB10))
 script='set -e\nlog_info() { :; }\nlog_error() { echo "$1" >&2; }\nreadonly CLUSTER_CI_SUBMITTED_LOCAL="${IS_LOCAL:-0}"\n'+source[begin:end]+'\nprintf "%s" "$CLUSTER_CI_HOST_PROFILE"\n'
 result=subprocess.run(['bash','-c',script],env=env,text=True,capture_output=True)
 assert result.returncode==0,result.stderr
 assert json.loads(result.stdout)==GB10


def test_malformed_hardware_profile_fails_closed(tmp_path,monkeypatch):
 monkeypatch.setenv('CLUSTER_CI_HOST_PROFILE','[]')
 with patch('src.runner.branch_executor.subprocess.run') as run,pytest.raises(ValueError):
  DockerRunner().run_container('synthetic','synthetic','synthetic',str(tmp_path),str(tmp_path),resources={'ram_gb':10,'vram_gb':100})
 run.assert_not_called()
