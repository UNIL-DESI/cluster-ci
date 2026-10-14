"""Run the dashboard's actual parser and executor collection in JavaScript."""
import json
import shutil
import subprocess
from pathlib import Path
import pytest

NODE = shutil.which('node')
pytestmark = pytest.mark.skipif(NODE is None, reason='Dashboard JavaScript checks require Node')
TEMPLATE = Path(__file__).resolve().parents[1] / 'src/scheduler/templates/dashboard.html'
WORKERS = [dict(worker_id='w-alpha', hostname='worker-alpha'),
           dict(worker_id='w-beta', hostname='worker-beta')]


def collect(nodes, plans=None, workers=None, home='w-beta'):
    source = TEMPLATE.read_text()
    parser = source[source.index('        function parseJobPlan(job) {'):
                    source.index('        // Generate clean Mermaid diagram')]
    block = source[source.index('                const executorsPerWorker = {};'):
                   source.index('                // Calculate busy count')]
    job = dict(job_id='synthetic', parallel_mode=1, home_worker=home,
               repo='example/synthetic', username='researcher', is_local=1,
               nodes=nodes, plan_json=dict(defaults={}, nodes=plans or []))
    script = parser + '\nconst onlineWorkers = ' + json.dumps(workers or WORKERS) + ';\n'
    script += 'const activeRuns = [' + json.dumps(job) + '];\n'
    script += "const v3DetailsMap = {synthetic: parseJobPlan(activeRuns[0])};\n"
    script += 'v3DetailsMap.synthetic.rawJob = activeRuns[0];\n'
    script += block + '\nconsole.log(JSON.stringify(executorsPerWorker));'
    r = subprocess.run([NODE, '-e', script], check=True, text=True, capture_output=True)
    return json.loads(r.stdout)


@pytest.mark.parametrize('identity', ['hostname', 'worker_id'])
def test_real_stage_assignments_and_resources(identity):
    nodes = [dict(name='train', status='running', machine=WORKERS[0][identity],
                  worker_id='w-alpha', runner_id='train'),
             dict(name='prepare', status='running', machine=WORKERS[1][identity],
                  worker_id='w-beta', runner_id='prepare'),
             dict(name='next', status='pending')]
    plans = [dict(name='train', resources=dict(cpus=8, gpus=1, ram_gb=10, vram_gb=100)),
             dict(name='prepare', resources=dict(cpus=8, gpus=0, ram_gb=16, vram_gb=0))]
    cards = collect(nodes, plans)
    train, = cards['w-alpha']
    prepare, = cards['w-beta']
    assert train['nodeName'] == 'train' and train['gpus'] == 1
    assert train['cpus'] == 8 and train['ramGb'] + train['vramGb'] == 110
    assert prepare['nodeName'] == 'prepare' and prepare['gpus'] == 0
    assert prepare['cpus'] == 8 and prepare['ramGb'] == 16
    assert prepare['isHome'] and not train['isHome']
    assert all(x['type'] == 'v3_node' for entries in cards.values() for x in entries)


def test_hostname_only_legacy_assignment():
    cards = collect([dict(name='train', status='running', machine='worker-alpha')])
    assert [x['nodeName'] for x in cards['w-alpha']] == ['train']
    assert cards['w-beta'] == []


def test_home_assignment_does_not_invent_coordinator():
    assert collect([dict(name='next', status='pending')]) == {'w-alpha': [], 'w-beta': []}


def test_stable_id_wins_over_old_hostname():
    cards = collect([dict(name='train', status='running', worker_id='w-alpha', machine='old-host')], home='worker-alpha')
    assert cards['w-alpha'][0]['isHome']
    assert cards['w-beta'] == []


def test_worker_reported_executors_preserved_and_deduplicated():
    workers = [dict(WORKERS[0], active_executors=[dict(runner_id='train', job_id='synthetic'),
               dict(runner_id='other', node_name='other', cpus=2, gpus=0, ram_gb=3)]), WORKERS[1]]
    cards = collect([dict(name='train', status='running', machine='worker-alpha',
                          worker_id='w-alpha', runner_id='train')], workers=workers)
    assert [x['runnerId'] for x in cards['w-alpha']] == ['train', 'other']
