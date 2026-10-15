"""Exercise worker execution from outside the checkout, as in systemd."""
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def test_script_entrypoint_can_preserve_private_node_after_stop(tmp_path):
    script = r'''
import json,os,runpy,sys,shutil,zipfile
from pathlib import Path
from unittest.mock import Mock,patch
root=Path(sys.argv[1]);scratch=Path(sys.argv[2])
# Installed service exposes src/, not the checkout root. The script directory
# is also importable, matching direct execution of worker_agent.py.
sys.path=[str(root/'src'),str(root/'src/scheduler')]+[p for p in sys.path if p and str(root) not in p]
os.environ.update(HEADNODE_URL='http://127.0.0.1:1',CLUSTER_CI_BASE_DIR=str(scratch),ROLE='worker')
worker=runpy.run_path(str(root/'src/scheduler/worker_agent.py'),run_name='worker_import_qualification')
from src.runner import local_workspace
repo='example/synthetic';job='synthetic-job';runner='synthetic-runner'
source=local_workspace.private_job_root(Path(worker['REPOS_DIR'])/'_local'/repo,job)/'source';source.mkdir(parents=True)
workspace=scratch/'workspace';workspace.mkdir();(workspace/'outputs').mkdir()
(workspace/'outputs/checkpoint.txt').write_text('synthetic checkpoint')
(workspace/'private').mkdir();(workspace/'private/input.txt').write_text('SYNTHETIC_PRIVATE_INPUT')
(workspace/'.env').write_text('SYNTHETIC_SECRET=never_publish')
(workspace/'dvc.yaml').write_text('stages:\n  train:\n    cmd: true\n    outs:\n      - outputs:\n          cache: false\n')
record=local_workspace.runner_record_path(source,runner)
record.write_text(json.dumps(dict(source=str(source),workspace=str(workspace),node='train',job_id=job)))
preserved=[]
def capture(path,job_id,node,url,token):
 target=scratch/'headnode-result.zip';shutil.copyfile(path,target);preserved.append(target);return True
# Hardware stop is simulated; archive generation and private output filtering
# run through the real preservation code and its nested package imports.
with patch('subprocess.run',return_value=Mock(returncode=0,stdout='',stderr='')) as docker,patch.object(local_workspace,'upload_node_archive',side_effect=capture):
 worker['_stop_runner_and_preserve'](runner,None,0,dict(is_local=True,repo=repo,job_id=job))
 assert docker.call_count>0 and all(c.args[0][0]=='docker' for c in docker.call_args_list)
assert len(preserved)==1 and not record.exists()
with zipfile.ZipFile(preserved[0]) as z:
 assert z.read('outputs/checkpoint.txt')==b'synthetic checkpoint'
 assert set(z.namelist())=={'dvc.lock','outputs/checkpoint.txt'}
print('PRIVATE_PRESERVATION_OK')
'''
    env=dict(os.environ)
    env.pop('PYTHONPATH',None)
    result=subprocess.run([sys.executable,'-I','-c',script,str(ROOT),str(tmp_path)],
                          cwd=tmp_path,env=env,text=True,capture_output=True)
    assert result.returncode==0,result.stdout+result.stderr
    assert 'PRIVATE_PRESERVATION_OK' in result.stdout
