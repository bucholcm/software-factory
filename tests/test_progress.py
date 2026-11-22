import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from factory.cli import status
from factory.config import write_json
from factory.progress import project, summary, timelines
from factory.store import Store
from scripts.status_panel import duration, render


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.job = {'id': 'job', 'task_id': 'synthetic', 'stage': 'testing', 'runtime': 'codex',
                    'revision': 'a'*40, 'reviewed_revision': '', 'review_result': '',
                    'tests_passed': False, 'created_at': 90, 'started_at': 120,
                    'next_run': 0, 'attempt': 2, 'attempt_dir': 'attempt', 'fix_count': 1}
        self.health = {'status': 'running', 'ready': True}
        self.timeline = {'first_started_at': 100, 'attempts_started': 5}

    def value(self, now=160, live=True):
        return project(self.job, self.health, self.timeline, now=now, worker_alive=live)

    def test_active_attempt_time_increases_and_verified_steps_are_real(self):
        first = self.value()
        later = self.value(now=170)
        self.assertEqual('working', first['activity'])
        self.assertEqual(40, first['attempt_elapsed_seconds'])
        self.assertEqual(50, later['attempt_elapsed_seconds'])
        self.assertEqual(70, first['elapsed_seconds'])
        self.assertEqual(1, first['verified_steps'])
        self.assertEqual(['passed', 'current', 'pending'], [x['state'] for x in first['milestones']])
        self.assertIsNone(first['tokens'])
        self.assertEqual('not_collected', first['token_status'])

    def test_test_fix_review_loop_invalidates_earlier_completion(self):
        self.job.update(stage='reviewing', tests_passed=True)
        self.assertEqual(2, self.value()['verified_steps'])
        self.job.update(stage='fixing', tests_passed=False, review_result='changes_requested', reviewed_revision='a'*40)
        self.assertEqual(0, self.value()['verified_steps'])
        self.assertEqual(1, self.value()['fix_cycles'])
        self.job.update(stage='ready', tests_passed=True, review_result='approved', reviewed_revision='b'*40)
        self.assertEqual('awaiting_verification', self.value(live=False)['activity'])
        self.assertEqual(2, self.value(live=False)['verified_steps'])

    def test_delivery_time_freezes_despite_repeated_policy_updates(self):
        self.job.update(stage='ready', tests_passed=True, reviewed_revision='a'*40, review_result='approved', updated_at=999)
        self.timeline['delivered_at'] = 150
        self.assertEqual(60, self.value(now=160, live=False)['elapsed_seconds'])
        self.assertEqual(60, self.value(now=990, live=False)['elapsed_seconds'])
        self.assertEqual('ready_for_delivery', self.value(live=False)['activity'])
        self.assertEqual(3, self.value(live=False)['verified_steps'])

    def test_total_age_does_not_reset_when_first_worker_starts(self):
        self.timeline = {}
        self.job.update(stage='implementing', attempt_dir='', started_at=0)
        queued = self.value(live=False)
        self.timeline['first_started_at'] = 160
        self.job.update(attempt_dir='attempt', started_at=160)
        started = self.value()
        self.assertEqual(70, queued['elapsed_seconds'])
        self.assertEqual(queued['elapsed_seconds'], started['elapsed_seconds'])
        self.assertEqual(0, started['attempt_elapsed_seconds'])

    def test_integrated_clock_includes_wait_after_review_and_integration(self):
        self.job.update(stage='integrated', tests_passed=True, reviewed_revision='a'*40, review_result='approved')
        self.timeline.update(delivered_at=150, integrated_at=300)
        self.assertEqual(210, self.value(now=990, live=False)['elapsed_seconds'])

    def test_stale_review_is_never_printed_as_approved(self):
        self.job.update(stage='ready', tests_passed=True, reviewed_revision='b'*40, review_result='approved')
        job = {**self.job, 'progress': self.value(live=False)}
        snapshot = {**self.health, 'jobs': [job], 'activity': summary([job])}
        text = render(snapshot, 'demo', now=160)
        self.assertIn('Review: stale', text)
        self.assertNotIn('Review: approved', text)

    def test_orphaned_owned_process_group_stays_visible_as_running_work(self):
        progress = project(self.job, self.health, self.timeline, now=160, group_alive=True)
        self.assertEqual('orphaned_worker', progress['activity'])
        self.assertEqual(40, progress['attempt_elapsed_seconds'])
        job = {**self.job, 'progress': progress}
        snapshot = {**self.health, 'jobs': [job], 'activity': summary([job])}
        text = render(snapshot, 'demo', now=160)
        self.assertIn('WORK IN PROGRESS', text)
        self.assertIn('1 verified live', text)
        self.assertIn('owned process group is still alive', text)

    def test_retry_queue_and_completion_reconciliation_do_not_claim_working(self):
        self.job.update(next_run=175)
        self.assertEqual('retry_wait', self.value(live=False)['activity'])
        self.assertEqual(15, self.value(live=False)['retry_in_seconds'])
        self.job['next_run'] = 0
        self.assertEqual('reconciling', self.value(live=False)['activity'])
        self.job['attempt_dir'] = ''
        self.assertEqual('queued', self.value(live=False)['activity'])
        self.assertIsNone(self.value(live=False)['attempt_elapsed_seconds'])

    def test_stale_supervisor_and_stalled_worker_are_explicit(self):
        self.health['status'] = 'unresponsive'
        self.assertEqual('working_supervisor_unavailable', self.value()['activity'])
        self.assertEqual('supervisor_unavailable', self.value(live=False)['activity'])
        self.job.update(stage='blocked', resume_stage='testing')
        self.assertEqual('blocked_worker_alive', self.value()['activity'])
        self.assertEqual('blocked', self.value(live=False)['activity'])

    def test_bad_or_missing_timestamps_never_fabricate_runtime(self):
        self.job.update(created_at=float('nan'), started_at=-1)
        self.timeline = {}
        self.assertIsNone(self.value()['elapsed_seconds'])
        self.assertIsNone(self.value()['attempt_elapsed_seconds'])
        self.job.update(created_at=190, started_at=180)
        self.assertEqual(0, self.value()['elapsed_seconds'])
        self.assertEqual(0, self.value()['attempt_elapsed_seconds'])

    def test_summary_uses_verified_job_activity(self):
        active = {'progress': self.value()}
        self.assertEqual('working', summary([active])['state'])
        self.job.update(stage='blocked')
        stalled = {'progress': self.value(live=False)}
        self.assertEqual(1, summary([active, stalled])['stalled'])
        self.assertEqual('stalled', summary([stalled])['state'])
        self.assertEqual('idle', summary([])['state'])

    def test_status_enriches_old_native_heartbeat_using_journal_without_private_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            config = {'state_dir': directory, 'repo': directory, 'integration_policy': 'disabled'}
            store = Store(directory)
            job = store.enqueue('synthetic', directory, 'factory/synthetic', payload={'requirements': 'synthetic-private'})
            store.update(job['id'], stage='testing', pid=321, process_identity='worker', started_at=120, revision='a'*40)
            with store.connect() as db:
                db.execute('UPDATE jobs SET created_at=? WHERE id=?', (90, job['id']))
                db.execute('INSERT INTO events(job_id,stage,code,created_at) VALUES(?,?,?,?)', (job['id'], 'implementing', 'worker_started', 100))
            write_json(Path(directory)/'status.json', {'status': 'running', 'ready': True, 'pid': 123,
                                                       'process_identity': 'supervisor', 'updated_at': 159})
            with patch('factory.cli.time.time', return_value=160), patch('factory.cli.process_matches', return_value=True):
                value = status(config)
            progress = value['jobs'][0]['progress']
            self.assertEqual(70, progress['elapsed_seconds'])
            self.assertEqual(1, progress['attempts_started'])
            self.assertEqual('working', value['activity']['state'])
            self.assertNotIn('synthetic-private', json.dumps(value))
            self.assertNotIn('process_identity', value['jobs'][0])
            self.assertEqual(100, timelines(store)[job['id']]['first_started_at'])

    def test_panel_prioritizes_active_jobs_over_old_completed_history(self):
        active = {**self.job, 'progress': self.value()}
        jobs = [active] + [dict(active, task_id='old-'+str(n), stage='integrated',
                              progress={**self.value(live=False), 'activity': 'completed', 'worker_alive': False}) for n in range(10)]
        snapshot = {**self.health, 'jobs': jobs, 'activity': summary(jobs), 'updated_at': 159}
        text = render(snapshot, 'demo', now=160)
        self.assertIn('WORK IN PROGRESS', text)
        self.assertIn('synthetic  ·  testing', text)
        self.assertIn('Attempt: 00m 40s', text)
        self.assertIn('[1/3 verified]', text)
        self.assertIn('Tokens: not collected', text)
        self.assertNotIn('\x1b', text)
        self.assertEqual('unknown', duration(None))
        self.assertEqual('1h 01m 01s', duration(3661))


if __name__ == '__main__':
    unittest.main()
