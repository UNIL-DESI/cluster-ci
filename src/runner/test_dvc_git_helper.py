import unittest
import os
import shutil
import tempfile
from pathlib import Path
from ruamel.yaml import YAML

# Add src to sys.path to import dvc_git_helper
import sys
sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..'))
from src.runner.dvc_git_helper import inject_cache_false, get_cache_false_paths

class TestDVCGitHelper(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.dvc_yaml = os.path.join(self.test_dir, 'dvc.yaml')
        self.yaml = YAML()

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_inject_shorthand(self):
        content = {
            'stages': {
                'train': {
                    'cmd': 'python train.py',
                    'metrics': ['metrics.json'],
                    'plots': ['plots.csv']
                }
            }
        }
        with open(self.dvc_yaml, 'w') as f:
            self.yaml.dump(content, f)

        inject_cache_false(self.dvc_yaml)

        with open(self.dvc_yaml, 'r') as f:
            data = self.yaml.load(f)

        self.assertEqual(data['stages']['train']['metrics'][0]['metrics.json']['cache'], False)
        self.assertEqual(data['stages']['train']['plots'][0]['plots.csv']['cache'], False)

    def test_inject_longhand(self):
        content = {
            'stages': {
                'train': {
                    'cmd': 'python train.py',
                    'metrics': [{'metrics.json': {'cache': True}}],
                    'plots': [{'plots.csv': {}}]
                }
            }
        }
        with open(self.dvc_yaml, 'w') as f:
            self.yaml.dump(content, f)

        inject_cache_false(self.dvc_yaml)

        with open(self.dvc_yaml, 'r') as f:
            data = self.yaml.load(f)

        self.assertEqual(data['stages']['train']['metrics'][0]['metrics.json']['cache'], False)
        self.assertEqual(data['stages']['train']['plots'][0]['plots.csv']['cache'], False)

    def test_get_paths(self):
        content = {
            'stages': {
                'train': {
                    'metrics': [{'m1.json': {'cache': False}}, {'m2.json': {'cache': True}}],
                    'plots': [{'p1.csv': {'cache': False}}]
                }
            }
        }
        with open(self.dvc_yaml, 'w') as f:
            self.yaml.dump(content, f)

        paths = get_cache_false_paths(self.dvc_yaml)
        self.assertIn('m1.json', paths)
        self.assertIn('p1.csv', paths)
        self.assertNotIn('m2.json', paths)

    def test_get_paths_with_wdir(self):
        content = {
            'stages': {
                'train': {
                    'wdir': 'results',
                    'metrics': [{'m1.json': {'cache': False}}]
                }
            }
        }
        with open(self.dvc_yaml, 'w') as f:
            self.yaml.dump(content, f)

        paths = get_cache_false_paths(self.dvc_yaml)
        self.assertIn('results/m1.json', paths)


from unittest.mock import patch, MagicMock

class TestSyncMetrics(unittest.TestCase):
    @patch('src.runner.dvc_git_helper.os.path.exists')
    @patch('src.runner.dvc_git_helper.os.path.isfile')
    @patch('src.runner.dvc_git_helper.os.path.getsize')
    @patch('src.runner.dvc_git_helper.get_cache_false_paths')
    @patch('src.runner.dvc_git_helper.subprocess.run')
    def test_sync_metrics_both_staged(self, mock_run, mock_get_paths, mock_getsize, mock_isfile, mock_exists):
        mock_exists.side_effect = lambda path: path == 'dvc.lock' or path == 'dvc.yaml'
        mock_isfile.return_value = True
        mock_getsize.return_value = 1000
        mock_get_paths.return_value = ['metrics.json']
        
        def run_side_effect(cmd, *args, **kwargs):
            res = MagicMock()
            cmd_str = ' '.join(cmd) if isinstance(cmd, list) else cmd
            if 'diff' in cmd_str:
                res.returncode = 1
            else:
                res.returncode = 0
            return res
            
        mock_run.side_effect = run_side_effect
        
        from src.runner.dvc_git_helper import sync_metrics
        sync_metrics()
        
        commit_calls = [c for c in mock_run.call_args_list if len(c[0]) > 0 and isinstance(c[0][0], list) and 'commit' in c[0][0]]
        self.assertEqual(len(commit_calls), 1)
        self.assertIn('chore(ci): auto-sync metrics and dvc.lock [skip ci]', commit_calls[0][0][0])

    @patch('src.runner.dvc_git_helper.os.path.exists')
    @patch('src.runner.dvc_git_helper.os.path.isfile')
    @patch('src.runner.dvc_git_helper.get_cache_false_paths')
    @patch('src.runner.dvc_git_helper.subprocess.run')
    def test_sync_metrics_only_lock_staged(self, mock_run, mock_get_paths, mock_isfile, mock_exists):
        mock_exists.side_effect = lambda path: path == 'dvc.lock'
        mock_isfile.return_value = False
        mock_get_paths.return_value = ['metrics.json']
        
        def run_side_effect(cmd, *args, **kwargs):
            res = MagicMock()
            cmd_str = ' '.join(cmd) if isinstance(cmd, list) else cmd
            if 'diff' in cmd_str and 'dvc.lock' in cmd_str:
                res.returncode = 1
            elif 'diff' in cmd_str and 'metrics.json' in cmd_str:
                res.returncode = 0
            elif 'diff' in cmd_str and '--cached' in cmd_str and 'metrics.json' not in cmd_str:
                res.returncode = 1
            else:
                res.returncode = 0
            return res
            
        mock_run.side_effect = run_side_effect
        
        from src.runner.dvc_git_helper import sync_metrics
        sync_metrics()
        
        commit_calls = [c for c in mock_run.call_args_list if len(c[0]) > 0 and isinstance(c[0][0], list) and 'commit' in c[0][0]]
        self.assertEqual(len(commit_calls), 1)
        self.assertIn('chore(ci): auto-sync dvc.lock [skip ci]', commit_calls[0][0][0])

    @patch('src.runner.dvc_git_helper.os.path.exists')
    @patch('src.runner.dvc_git_helper.os.path.isfile')
    @patch('src.runner.dvc_git_helper.os.path.getsize')
    @patch('src.runner.dvc_git_helper.get_cache_false_paths')
    @patch('src.runner.dvc_git_helper.subprocess.run')
    def test_sync_metrics_only_metrics_staged(self, mock_run, mock_get_paths, mock_getsize, mock_isfile, mock_exists):
        mock_exists.side_effect = lambda path: path == 'dvc.yaml'
        mock_isfile.return_value = True
        mock_getsize.return_value = 1000
        mock_get_paths.return_value = ['metrics.json']
        
        def run_side_effect(cmd, *args, **kwargs):
            res = MagicMock()
            cmd_str = ' '.join(cmd) if isinstance(cmd, list) else cmd
            if 'diff' in cmd_str and 'metrics.json' in cmd_str:
                res.returncode = 1
            elif 'diff' in cmd_str and '--cached' in cmd_str and 'dvc.lock' not in cmd_str:
                res.returncode = 1
            else:
                res.returncode = 0
            return res
            
        mock_run.side_effect = run_side_effect
        
        from src.runner.dvc_git_helper import sync_metrics
        sync_metrics()
        
        commit_calls = [c for c in mock_run.call_args_list if len(c[0]) > 0 and isinstance(c[0][0], list) and 'commit' in c[0][0]]
        self.assertEqual(len(commit_calls), 1)
        self.assertIn('chore(ci): auto-sync metrics [skip ci]', commit_calls[0][0][0])

    @patch('src.runner.dvc_git_helper.os.path.exists')
    @patch('src.runner.dvc_git_helper.os.path.isfile')
    @patch('src.runner.dvc_git_helper.get_cache_false_paths')
    @patch('src.runner.dvc_git_helper.subprocess.run')
    def test_sync_metrics_no_changes(self, mock_run, mock_get_paths, mock_isfile, mock_exists):
        mock_exists.return_value = False
        mock_isfile.return_value = False
        mock_get_paths.return_value = []
        
        mock_run.return_value = MagicMock(returncode=0)
        
        from src.runner.dvc_git_helper import sync_metrics
        sync_metrics()
        
        commit_calls = [c for c in mock_run.call_args_list if len(c[0]) > 0 and isinstance(c[0][0], list) and 'commit' in c[0][0]]
        self.assertEqual(len(commit_calls), 0)


if __name__ == '__main__':
    unittest.main()
