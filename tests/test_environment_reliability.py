import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from factory.adapters import _agent, _gates, runtime_environment
from factory.config import git
from factory.environment import ExecutionFailure, prepare
from factory.reliability import observe, report, sync, dispatch
from factory.store import Store
from factory.supervisor import Supervisor
from factory.worker import sanitize_result


class EnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='factory-project with spaces-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        for args in (['init', '-b', 'main'], ['config', 'user.name', 'Test'],
                     ['config', 'user.email', 'test@local.invalid']):
            subprocess.run(['git', '-C', str(self.repo), *args], check=True, capture_output=True)
        (self.repo / '.gitignore').write_text('.venv/\ndependencies-ready\n')
        (self.repo / 'manifest.txt').write_text('synthetic dependencies v1')
        git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-m', 'initial')
        self.job = {'id': 'synthetic', 'task_id': 'synthetic', 'stage': 'implementing',
                    'runtime': 'codex', 'worktree': str(self.repo), 'payload': {}}
        self.config = {'state_dir': str(self.root / 'state'), 'environment': {
            'fingerprint_files': ['manifest.txt'],
            'bootstrap_commands': [[sys.executable, '-c', "from pathlib import Path; Path('dependencies-ready').write_text('ready')"]],
            'checks': [[sys.executable, '-c', "from pathlib import Path; raise SystemExit(not Path('dependencies-ready').exists())"]],
        }}

    def prepare(self):
        return prepare(self.job, self.config, self.root, runtime_environment())

    def test_missing_system_dependency_never_starts_model_or_retry_loop(self):
        self.config['environment']['required_executables'] = ['synthetic-missing-ocr-tool']
        self.config['codex_executable'] = 'codex'
        attempt = self.root / 'attempt'
        attempt.mkdir()
        with patch('factory.adapters.run_agent') as model:
            with self.assertRaisesRegex(ExecutionFailure, 'environment_executable_missing'):
                _agent(self.job, self.config, attempt / 'result.json')
            model.assert_not_called()
        with Supervisor({'state_dir': self.config['state_dir']}, lambda *_: []) as supervisor:
            job = supervisor.store.enqueue('synthetic', self.repo, 'factory/synthetic')
            job = supervisor.store.update(job['id'], stage='testing', attempt_dir='attempt-1')
            supervisor._finish(job, {'status': 'failed', 'blocker': 'environment_executable_missing',
                                     'diagnostic': {'phase': 'preflight', 'command_index': 0}})
            result = supervisor.store.get(job['id'])
            self.assertEqual('blocked', result['stage'])
            self.assertEqual(0, result['fix_count'])
            self.assertEqual('testing', result['resume_stage'])

    def test_fresh_worktree_bootstraps_once_and_restart_reuses_verified_environment(self):
        self.prepare()
        receipt = next((self.root / 'state/environments').glob('*/receipt.json'))
        original = receipt.stat().st_mtime_ns
        self.prepare()
        self.assertEqual(original, receipt.stat().st_mtime_ns)
        # Receipt alone cannot mask removed dependencies.
        (self.repo / 'dependencies-ready').unlink()
        self.prepare()
        self.assertTrue((self.repo / 'dependencies-ready').exists())
        (self.repo / 'manifest.txt').write_text('synthetic dependencies v2')
        self.prepare()
        self.assertNotEqual(original, receipt.stat().st_mtime_ns)

    def test_explicit_python_creates_venv_in_path_with_spaces(self):
        self.config['environment']['python'] = sys.executable
        self.config['environment']['checks'] = [['{venv_python}', '-I', '-c', 'import sys; assert sys.version_info >= (3,11)']]
        self.config['environment']['bootstrap_commands'] = []
        env = self.prepare()
        self.assertEqual(str((self.repo / '.venv').resolve()), env['VIRTUAL_ENV'])
        self.assertEqual('', git(self.repo, 'status', '--porcelain'))

    def test_incompatible_existing_python_is_blocked_and_preserved(self):
        self.config['environment']['minimum_python'] = [99, 0]
        self.config['environment']['python'] = sys.executable
        with self.assertRaisesRegex(ExecutionFailure, 'environment_python_mismatch'):
            self.prepare()
        self.assertTrue((self.repo / '.venv/bin/python3').exists())

    def test_bad_bootstrap_and_symlink_environment_are_never_cached(self):
        self.config['environment']['bootstrap_commands'] = [[sys.executable, '-c', 'raise SystemExit(7)']]
        with self.assertRaises(ExecutionFailure) as found:
            self.prepare()
        self.assertEqual({'phase': 'bootstrap', 'command_index': 0, 'exit_code': 7}, found.exception.diagnostic)
        self.assertFalse(list((self.root / 'state/environments').glob('*/receipt.json')))
        (self.repo / '.venv').symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(ExecutionFailure, 'environment_venv_unsafe'):
            self.prepare()

    def test_bootstrap_cannot_modify_source_or_fingerprint_symlink(self):
        self.config['environment']['bootstrap_commands'] = [[sys.executable, '-c', "from pathlib import Path; Path('manifest.txt').write_text('changed')"]]
        with self.assertRaisesRegex(ExecutionFailure, 'environment_modified_source'):
            self.prepare()
        (self.repo / 'manifest.txt').unlink()
        (self.repo / 'manifest.txt').symlink_to(self.repo / '.gitignore')
        with self.assertRaises(RuntimeError):
            self.prepare()

    def test_dependency_checks_cannot_modify_clean_or_already_dirty_source(self):
        for cached in (False, True):
            with self.subTest(cached=cached):
                if cached:
                    self.config['environment']['checks'] = [[sys.executable, '-c', 'pass']]
                    self.prepare()
                (self.repo / 'manifest.txt').write_text('already dirty')
                self.config['environment']['checks'] = [[sys.executable, '-c', "from pathlib import Path; Path('manifest.txt').write_text('mutated')"]]
                with self.assertRaisesRegex(ExecutionFailure, 'environment_modified_source'):
                    self.prepare()
                (self.repo / 'manifest.txt').write_text('synthetic dependencies v1')

    def test_cached_probe_cannot_mutate_source(self):
        probe = "from pathlib import Path; p=Path('dependencies-ready'); Path('manifest.txt').write_text('changed') if p.read_text() == 'mutate' else None"
        self.config['environment']['checks'] = [[sys.executable, '-c', probe]]
        self.prepare()
        (self.repo / 'dependencies-ready').write_text('mutate')
        with self.assertRaisesRegex(ExecutionFailure, 'environment_modified_source'):
            self.prepare()

    def test_retest_recovery_preserves_revision_and_requires_fresh_gates_and_review(self):
        store = Store(self.root / 'state')
        job = store.enqueue('retest', self.repo, 'factory/retest')
        revision = git(self.repo, 'rev-parse', 'HEAD')
        store.update(job['id'], stage='failed', resume_stage='fixing', revision=revision,
                     blocker='agent_process_failed', tests_passed=True, reviewed_revision=revision)
        resumed = store.resume(job['id'], retest=True)
        self.assertEqual('testing', resumed['stage'])
        self.assertEqual(revision, resumed['revision'])
        self.assertFalse(resumed['tests_passed'])
        self.assertEqual('', resumed['reviewed_revision'])
        store.update(job['id'], stage='failed', resume_stage='fixing', review_result='changes_requested')
        with self.assertRaisesRegex(ValueError, 'retest_requires_unreviewed_test_failure'):
            store.resume(job['id'], retest=True)
        store.update(job['id'], review_result='')
        (self.repo / 'manifest.txt').write_text('uncommitted')
        with self.assertRaisesRegex(ValueError, 'retest_revision_changed'):
            store.resume(job['id'], retest=True)

    def test_environment_repair_cli_accepts_completed_pid_but_refuses_live_process(self):
        from factory.cli import main
        config = {**self.config, 'repo': str(self.repo), 'runtime': 'codex', 'integration_policy': 'disabled'}
        path = self.root / 'config.json'
        path.write_text(json.dumps(config))
        store = Store(config['state_dir'])
        job = store.enqueue('cli-repair', self.repo, 'factory/cli-repair')
        store.update(job['id'], stage='blocked', resume_stage='testing', pid=99999999, process_identity='old')
        args = ['factory', '--config', str(path), 'prepare-environment', job['id']]
        with patch('sys.argv', args), patch('builtins.print'):
            main()
        store.update(job['id'], pid=os.getpid())
        with patch('sys.argv', args), patch('factory.cli.process_matches', return_value=True):
            with self.assertRaisesRegex(ValueError, 'environment_repair_requires_stopped_job'):
                main()

    def test_gate_failure_contains_only_safe_context(self):
        del self.config['environment']
        self.config['quality_gates'] = [[sys.executable, '-c', "print('synthetic-private-value'); raise SystemExit(9)"]]
        self.job.update(stage='testing', revision=git(self.repo, 'rev-parse', 'HEAD'))
        self.job['payload'] = {'base_revision': self.job['revision']}
        attempt = self.root / 'attempt'
        attempt.mkdir()
        with self.assertRaises(ExecutionFailure) as found:
            _gates(self.job, self.config, attempt / 'result.json')
        self.assertEqual({'phase': 'quality_gate', 'command_index': 0, 'exit_code': 9}, found.exception.diagnostic)
        self.assertNotIn('private', str(found.exception))

    def test_agent_process_missing_result_and_reported_failure_are_distinct(self):
        attempt = self.root / 'attempt'
        attempt.mkdir()
        config = {'codex_executable': 'codex'}
        with patch('factory.adapters.run_agent', return_value=subprocess.CompletedProcess([], 12)):
            with self.assertRaisesRegex(ExecutionFailure, 'agent_process_failed') as found:
                _agent(self.job, config, attempt / 'result.json')
            self.assertEqual(12, found.exception.diagnostic['exit_code'])
        with patch('factory.adapters.run_agent', return_value=subprocess.CompletedProcess([], 0)):
            with self.assertRaisesRegex(ExecutionFailure, 'agent_result_missing'):
                _agent(self.job, config, attempt / 'result.json')
        def failed(*args, **kwargs):
            (attempt / 'native-result.json').write_text(json.dumps({'status': 'failed', 'question': 'synthetic-private'}))
            return subprocess.CompletedProcess([], 0)
        with patch('factory.adapters.run_agent', side_effect=failed):
            _agent(self.job, config, attempt / 'result.json')
        self.assertEqual({'status': 'failed', 'blocker': 'agent_reported_failure'}, json.loads((attempt / 'result.json').read_text()))
        self.assertFalse((attempt / 'native-result.json').exists())


class FeedbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = {'state_dir': str(self.root / 'state'), 'reliability_tracker_repo': str(self.root / 'factory-tracker')}
        subprocess.run(['git', 'init', '-q', self.config['reliability_tracker_repo']], check=True)
        self.store = Store(self.config['state_dir'])
        self.job = self.store.enqueue('test', self.root, 'factory/test')
        self.job.update(stage='testing', attempt_dir='attempt-1')
        self.failure = {'status': 'failed', 'blocker': 'environment_executable_missing',
                        'diagnostic': {'phase': 'preflight', 'command_index': 0, 'stderr': 'synthetic-private'}}

    def test_feedback_survives_restart_deduplicates_completion_and_records_recurrence(self):
        observe(self.store, self.job, self.failure)
        observe(Store(self.config['state_dir']), self.job, self.failure)
        first = report(self.config)['incidents'][0]
        self.assertEqual(1, first['occurrences'])
        self.assertNotIn('synthetic-private', json.dumps(first))
        self.job['attempt_dir'] = 'attempt-2'
        observe(self.store, self.job, self.failure)
        self.assertEqual(2, report(self.config)['incidents'][0]['occurrences'])
        observe(self.store, self.job, {'status': 'completed'})
        self.assertIsNotNone(report(self.config)['incidents'][0]['recovered_at'])
        self.job['attempt_dir'] = 'attempt-3'
        observe(self.store, self.job, self.failure)
        self.assertIsNone(report(self.config)['incidents'][0]['recovered_at'])

    def test_multi_project_feedback_reuses_tracker_task_after_export_crash(self):
        observe(self.store, self.job, self.failure)
        other = Store(self.root / 'other')
        second = {**self.job, 'id': 'other-job'}
        observe(other, second, self.failure)
        self.config['reliability_sources'] = [str(self.root / 'other')]
        issues, calls = [], []
        def bd(config, action, *args, **kwargs):
            self.assertEqual(self.config['reliability_tracker_repo'], config['repo'])
            calls.append((action, args))
            if action == 'search':
                return issues
            if action == 'create':
                metadata = json.loads(next(a.split('=', 1)[1] for a in args if a.startswith('--metadata=')))
                issue = {'id': 'repair-1', 'status': 'open', 'metadata': metadata}
                issues.append(issue)
                return issue
        self.assertEqual(1, sync(self.config, bd)['updated'])
        self.assertEqual(0, sync(self.config, bd)['updated'])
        (self.root / 'state/reliability/beads-index.json').unlink()
        sync(self.config, bd)
        self.assertEqual(1, sum(action == 'create' for action, _ in calls))
        values = [args[-1] for action, args in calls if action == 'update']
        self.assertIn('"projects": 2', values[0])
        self.assertNotIn('synthetic-private', str(calls))

    def test_one_job_recovery_cannot_mask_another_unresolved_job(self):
        observe(self.store, self.job, self.failure)
        second = {**self.job, 'id': 'second', 'attempt_dir': 'second-attempt'}
        observe(self.store, second, self.failure)
        observe(self.store, second, {'status': 'completed'})
        self.assertIsNone(report(self.config)['incidents'][0]['recovered_at'])
        observe(self.store, self.job, {'status': 'completed'})
        self.assertIsNotNone(report(self.config)['incidents'][0]['recovered_at'])

    def test_invalid_numeric_source_fields_never_escape_to_reports(self):
        observe(self.store, self.job, self.failure)
        for field in ('occurrences', 'first_seen', 'last_seen', 'recovered_at'):
            with self.store.connect() as db:
                db.execute('UPDATE reliability_incidents SET ' + field + '=?', ('synthetic-private',))
            value = report(self.config)
            self.assertEqual([], value['incidents'])
            self.assertEqual(1, value['unavailable_sources'])
            self.assertNotIn('synthetic-private', json.dumps(value))
            with self.store.connect() as db:
                db.execute('UPDATE reliability_incidents SET ' + field + '=1')

    def test_concurrent_project_exports_share_target_tracker_lock(self):
        from concurrent.futures import ThreadPoolExecutor
        import time
        observe(self.store, self.job, self.failure)
        other_config = {**self.config, 'state_dir': str(self.root / 'second-state')}
        other = Store(other_config['state_dir'])
        observe(other, {**self.job, 'id': 'other-job'}, self.failure)
        issues = []
        def bd(config, action, *args, **kwargs):
            if action == 'search':
                snapshot = list(issues)
                time.sleep(0.03)
                return snapshot
            if action == 'create':
                metadata = json.loads(next(a.split('=', 1)[1] for a in args if a.startswith('--metadata=')))
                issue = {'id': 'repair-1', 'metadata': metadata, 'status': 'open'}
                issues.append(issue)
                return issue
        with ThreadPoolExecutor(2) as pool:
            list(pool.map(lambda config: sync(config, bd), [self.config, other_config]))
        self.assertEqual(1, len(issues))

    def test_automatic_repairs_require_separate_factory_and_reserve_unresolved_scope(self):
        app = self.root / 'application'
        factory = self.root / 'factory'
        for repo in (app, factory):
            repo.mkdir()
            for args in (['init', '-b', 'main'], ['config', 'user.name', 'Test'],
                         ['config', 'user.email', 'test@local.invalid']):
                subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True)
            (repo / 'README.md').write_text('synthetic')
            git(repo, 'add', '.')
            git(repo, 'commit', '-m', 'initial')
        target = {'role': 'factory-repair', 'repo': str(factory), 'state_dir': str(self.root / 'repair-state'),
                  'base_branch': 'main', 'runtime': 'codex', 'task_policy': 'source',
                  'integration_policy': 'disabled', 'max_active_jobs': 1,
                  'reliability_repair_paths': ['factory', 'tests']}
        path = self.root / 'repair-config.json'
        path.write_text(json.dumps(target))
        config = {**self.config, 'repo': str(app), 'reliability_repair_config': str(path),
                  'reliability_factory_repo': str(factory)}
        observe(self.store, self.job, self.failure)
        with patch('factory.cli.status', return_value={'status': 'running', 'ready': True}):
            value = dispatch(config)
            self.assertEqual('repair_dispatched', value['status'])
            self.assertEqual('repair_capacity_reserved', dispatch(config)['status'])
        job = Store(target['state_dir']).jobs()[0]
        self.assertEqual(['factory', 'tests'], job['payload']['allowed_paths'])
        self.assertNotIn('synthetic-private', job['payload']['requirements'])
        self.assertEqual('disabled', json.loads(path.read_text())['integration_policy'])
        target['repo'] = str(app)
        path.write_text(json.dumps(target))
        with self.assertRaisesRegex(ValueError, 'repair_requires_separate_factory_repository'):
            dispatch(config)

    def test_supervisor_faults_are_observed_without_duplicate_poll_counts(self):
        with Supervisor({'state_dir': self.config['state_dir']}, lambda *_: []) as supervisor:
            supervisor._block(self.job, 'worker_watchdog_timeout')
            supervisor._block(self.job, 'worker_watchdog_timeout')
        values = report(self.config)['incidents']
        self.assertEqual(1, values[0]['occurrences'])
        self.assertEqual('worker_watchdog_timeout', values[0]['blocker'])

    def test_old_or_missing_source_is_reported_as_unavailable(self):
        self.config['reliability_sources'] = [str(self.root / 'missing')]
        self.assertEqual(1, report(self.config)['unavailable_sources'])

    def test_free_text_never_survives_result_sanitization(self):
        result = sanitize_result({**self.failure, 'stderr': 'private',
                                  'diagnostic': {'phase': 'private', 'exit_code': True, 'command_index': 3, 'stderr': 'private'}})
        self.assertEqual({'command_index': 3}, result['diagnostic'])


if __name__ == '__main__':
    unittest.main()
