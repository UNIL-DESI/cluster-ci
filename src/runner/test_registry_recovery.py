import copy
import pytest
from src.runner.registry_recovery import recover_running
@pytest.mark.parametrize('status',['pending','assigned','running','preempted',None])
def test_live_or_unknown_preserved(status):
 r={'_local/org/repo':dict(status='running',job_ids=['j'])};before=copy.deepcopy(r)
 assert not recover_running(r,lambda j:dict(job_id=j,repo='org/repo',is_local=1,status=status,project_has_active_jobs=False),lambda n:False,100)
 assert r==before
def test_terminal_recovers_with_fresh_protection():
 r={'_local/org/repo':dict(status='running',job_ids=['j'])}
 assert recover_running(r,lambda j:dict(job_id=j,repo='org/repo',is_local=1,status='failed',project_has_active_jobs=False),lambda n:False,100)==['_local/org/repo']
 assert r['_local/org/repo']==dict(status='idle',job_ids=[],last_execution=100)
@pytest.mark.parametrize('case',['mounted','network','wrong_scope','old_registry','one_live','queued_successor','old_headnode'])
def test_fail_closed(case):
 r={'_local/org/repo':dict(status='running',job_ids=['j','k'])}
 if case=='old_registry':r['_local/org/repo'].pop('job_ids')
 before=copy.deepcopy(r)
 def lookup(j):
  if case=='network':raise OSError('unavailable')
  return dict(job_id=j,repo='org/repo',is_local=case!='wrong_scope',status='running' if case=='one_live' and j=='k' else 'completed',project_has_active_jobs=True if case=='queued_successor' else None if case=='old_headnode' else False)
 assert not recover_running(r,lookup,lambda n:case=='mounted',100)
 assert r==before
@pytest.mark.parametrize('status',['pending','assigned','running','failed','completed'])
def test_project_activity_query_is_scoped(status):
 import sqlite3
 from src.runner.registry_recovery import project_has_active_jobs
 conn=sqlite3.connect(':memory:');conn.execute('CREATE TABLE jobs(repo TEXT,is_local INTEGER,status TEXT)')
 conn.executemany('INSERT INTO jobs VALUES(?,?,?)',[('org/repo',1,status),('org/repo',0,'running'),('org/other',1,'running')])
 assert project_has_active_jobs(conn,'org/repo',True)==(status in ['pending','assigned','running'])
 conn.close()
def test_runner_finish_requires_project_confirmation(tmp_path,monkeypatch):
 import json
 from src.runner import gc_orchestrator as gc
 monkeypatch.setattr(gc,'get_registry_path',lambda:tmp_path/'registry.json')
 monkeypatch.setenv('JOB_ID','job-one');gc.update_running('_local/org/repo')
 monkeypatch.setenv('JOB_ID','job-two');gc.update_running('_local/org/repo')
 gc.update_idle('_local/org/repo',str(tmp_path/'absent-workspace'))
 r=json.loads((tmp_path/'registry.json').read_text())['_local/org/repo']
 assert r['status']=='running' and r['job_ids']==['job-one','job-two']
