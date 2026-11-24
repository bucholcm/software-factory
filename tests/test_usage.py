"""Synthetic offline runtime streams; no real prompts, sessions, or prices."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from factory.environment import ExecutionFailure
from factory.usage import Collector, JsonLines, MAX_EVENT, MAX_RECORD, MAX_STEPS, opencode_session_model, run_agent


def codex(**changes):
    usage = dict(input_tokens=100, cached_input_tokens=20, cache_write_input_tokens=10,
                 output_tokens=30, reasoning_output_tokens=5)
    usage.update(changes)
    return {'type': 'turn.completed', 'usage': usage}


def step(message='message-1', **changes):
    part = {'id': 'part-1', 'sessionID': 'session-1', 'messageID': message,
            'reason': 'stop', 'cost': 0,
            'tokens': {'input': 100, 'output': 30, 'reasoning': 5,
                       'cache': {'read': 20, 'write': 10}}}
    part.update(changes)
    return {'type': 'step_finish', 'sessionID': 'session-1', 'part': part}


class UsageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.number = 0

    def collector(self, runtime='codex', config=None):
        self.number += 1
        path = self.root / f'implementing-{self.number}-100' / 'usage.json'
        path.parent.mkdir()
        return Collector({'id': 'synthetic-job', 'task_id': 'synthetic-task', 'stage': 'implementing'},
                         runtime, path, config or {}, requested_model='requested-only')

    def run_stream(self, collector, code, prompt='', timeout=3):
        return run_agent([sys.executable, '-c', code], cwd=self.root, env=os.environ.copy(),
                         prompt=prompt, timeout=timeout, collector=collector)

    def emit(self, *events):
        return 'import json\n' + '\n'.join(f'print(json.dumps({event!r}), flush=True)' for event in events)

    def test_codex_subprocess_cumulative_snapshots_are_not_summed(self):
        collector = self.collector()
        self.run_stream(collector, self.emit({'type': 'thread.started', 'thread_id': 'thread-1'},
                                            codex(), codex(input_tokens=200, output_tokens=60)))
        result = json.loads(collector.path.read_text())
        self.assertEqual('complete', result['usage_status'])
        self.assertEqual({'input_tokens': 170, 'cached_input_tokens': 20,
                          'cache_write_input_tokens': 10, 'output_tokens': 60,
                          'reasoning_output_tokens': 5}, result['tokens'])
        self.assertEqual(260, result['total_tokens'])
        self.assertEqual('thread-1', result['session_id'])
        self.assertIsNone(result['groups'][0]['model'])
        self.assertIsNone(result['reported_session_model'])
        self.assertEqual('unpriced', result['estimated_cost']['status'])

    def test_codex_regression_and_invalid_subset_preserve_partial_coverage(self):
        for event in (codex(input_tokens=90), codex(input_tokens=10), codex(reasoning_output_tokens=31)):
            with self.subTest(event=event):
                collector = self.collector()
                collector.consume(codex())
                collector.consume(event)
                collector.finish('completed', 0)
                self.assertEqual('partial', collector.snapshot()['usage_status'])
                self.assertGreater(collector.snapshot()['parser']['invalid_events'], 0)
        for event, unknown in ((codex(input_tokens=10), 'input_tokens'),
                               (codex(reasoning_output_tokens=31), 'output_tokens')):
            with self.subTest(initial_invalid=event):
                collector = self.collector()
                collector.consume(event)
                collector.finish('completed', 0)
                self.assertIsNone(collector.snapshot()['tokens'][unknown])
                self.assertEqual('partial', collector.snapshot()['usage_status'])

    def test_opencode_step_identity_and_disjoint_counters(self):
        collector = self.collector('opencode')
        self.run_stream(collector, self.emit(step(), step(id='different-part-id'), step('message-2')))
        result = collector.snapshot()
        self.assertEqual('complete', result['usage_status'])
        self.assertEqual(2, len(collector.units))
        self.assertEqual({'input_tokens': 200, 'cached_input_tokens': 40,
                          'cache_write_input_tokens': 20, 'output_tokens': 70,
                          'reasoning_output_tokens': 10}, result['tokens'])
        self.assertEqual(330, result['total_tokens'])
        self.assertEqual('0', result['runtime_reported_cost']['amount'])
        self.assertEqual('runtime_reported_not_invoice', result['runtime_reported_cost']['kind'])

    def test_runtime_zero_cost_is_separate_from_nonzero_estimate(self):
        config = {'usage_pricing': {'version': 'synthetic-v1', 'rates': [
            {'provider': 'synthetic', 'model': 'test-model', 'currency': 'USD', 'unit_tokens': 1000,
             'input_per_unit': '1', 'cached_input_per_unit': '1', 'cache_write_per_unit': '1',
             'output_per_unit': '1', 'source_url': 'https://example.com/synthetic', 'effective_date': '2026-01-01'}]},
            'usage_price_assumptions': {'opencode': {'provider': 'synthetic', 'model': 'test-model'}}}
        collector = self.collector('opencode', config)
        collector.consume(step())
        collector.finish('completed', 0)
        result = collector.snapshot()
        self.assertEqual('0', result['runtime_reported_cost']['amount'])
        self.assertEqual('0.165', result['estimated_cost']['currencies'][0]['amount'])
        self.assertEqual('configured_assumption', result['estimated_cost']['lines'][0]['basis'])

    def test_missing_invalid_failed_and_timeout_are_not_zero(self):
        for event in (None, codex(input_tokens=None, cached_input_tokens=None,
                                   cache_write_input_tokens=None, output_tokens=None,
                                   reasoning_output_tokens=None),
                      codex(input_tokens=True, output_tokens=-1)):
            with self.subTest(event=event):
                collector = self.collector()
                if event is not None:
                    collector.consume(event)
                collector.finish('failed', 7)
                result = collector.snapshot()
                self.assertIn(result['usage_status'], ('partial', 'not_reported'))
                self.assertIsNone(result['tokens']['input_tokens'])
                self.assertIsNone(result['total_tokens'])
                self.assertNotEqual('estimated', result['estimated_cost']['status'])
        collector = self.collector()
        self.run_stream(collector, self.emit(codex()) + '\nraise SystemExit(7)')
        self.assertEqual('failed', collector.snapshot()['execution_status'])
        self.assertEqual('partial', collector.snapshot()['usage_status'])

    def test_unfinished_opencode_steps_remain_partial(self):
        collector = self.collector('opencode')
        collector.consume({'type': 'step_start', 'sessionID': 'session-1',
                           'part': {'messageID': 'unfinished'}})
        collector.consume(step())
        collector.finish('completed', 0)
        self.assertEqual('partial', collector.snapshot()['usage_status'])

    def test_pending_step_ids_and_deep_json_are_bounded_and_do_not_stop_drain(self):
        collector = self.collector('opencode')
        for n in range(MAX_STEPS + 20):
            collector.consume({'type': 'step_start', 'sessionID': 'session-1', 'part': {'messageID': 'message-' + str(n)}})
        self.assertEqual(MAX_STEPS, len(collector.open_steps))
        self.assertTrue(collector.incomplete)
        collector = self.collector()
        parser = JsonLines(collector)
        parser.feed(b'[' * 2000 + b'0' + b']' * 2000 + b'\n')
        parser.feed(json.dumps(codex()).encode() + b'\n')
        collector.finish('completed', 0)
        self.assertEqual(130, collector.snapshot()['total_tokens'])
        self.assertEqual('partial', collector.snapshot()['usage_status'])

    def test_partial_runtime_cost_is_a_known_subtotal_not_full_amount(self):
        collector = self.collector('opencode')
        collector.consume({'type': 'step_start', 'sessionID': 'session-1', 'part': {'messageID': 'unfinished'}})
        collector.consume(step(cost=0.25))
        collector.finish('timeout', -9)
        cost = collector.snapshot()['runtime_reported_cost']
        self.assertEqual('partial', cost['coverage'])
        self.assertEqual('0.25', cost['known_amount'])
        self.assertNotIn('amount', cost)

    def test_opencode_conflicting_duplicate_and_foreign_session_do_not_sum(self):
        collector = self.collector('opencode')
        collector.consume(step())
        collector.consume(step(cost=1))
        foreign = step('foreign-message')
        foreign['sessionID'] = 'foreign-session'
        foreign['part']['sessionID'] = 'foreign-session'
        collector.consume(foreign)
        collector.finish('completed', 0)
        result = collector.snapshot()
        self.assertEqual('partial', result['usage_status'])
        self.assertEqual(1, len(collector.units))
        self.assertEqual(100, result['tokens']['input_tokens'])
        self.assertEqual('session-1', result['session_id'])

    def test_partial_groups_keep_known_evidence_without_full_coverage(self):
        collector = self.collector('opencode')
        collector.consume(step())
        collector.consume(step('message-2', tokens={'input': None, 'output': 2,
                                                   'reasoning': 3, 'cache': {'read': 4, 'write': 5}}))
        collector.finish('completed', 0)
        result = collector.snapshot()
        self.assertEqual('partial', result['usage_status'])
        self.assertIsNone(result['tokens']['input_tokens'])
        self.assertEqual(40, result['tokens']['output_tokens'])
        self.assertIsNone(result['total_tokens'])
        self.assertEqual(179, result['known_tokens'])
        self.assertEqual(2, len(result['groups']))

    def test_huge_private_output_and_invalid_frames_are_drained_not_persisted(self):
        collector = self.collector()
        code = ('import sys\n'
                'sys.stderr.write("PRIVATE_STDERR_MARKER" * 100000)\n'
                'sys.stderr.flush()\n'
                f'sys.stdout.write("PRIVATE_STDOUT_MARKER" * {MAX_EVENT // 10} + "\\n")\n'
                'sys.stdout.write("{invalid JSON}\\n")\n'
                'sys.stdout.flush()\n' + self.emit(codex()))
        completed = self.run_stream(collector, code)
        raw = collector.path.read_text()
        self.assertNotIn('PRIVATE_', raw)
        self.assertLess(len(raw), MAX_RECORD)
        self.assertEqual('', completed.stdout)
        self.assertGreater(collector.snapshot()['parser']['oversized_events'], 0)
        self.assertGreater(collector.snapshot()['parser']['invalid_events'], 0)
        self.assertEqual('partial', collector.snapshot()['usage_status'])

    def test_long_stdin_is_fully_delivered_and_blocked_stdin_times_out(self):
        collector = self.collector()
        prompt = 'synthetic-long-input\n' * 100000
        code = (f'import sys\nassert len(sys.stdin.buffer.read()) == {len(prompt.encode())}\n' + self.emit(codex()))
        completed = self.run_stream(collector, code, prompt=prompt)
        self.assertEqual(0, completed.returncode)
        collector = self.collector()
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            self.run_stream(collector, 'import time; time.sleep(30)', prompt=prompt, timeout=.15)
        self.assertLess(time.monotonic() - started, 4)
        result = json.loads(collector.path.read_text())
        self.assertEqual('timeout', result['execution_status'])
        self.assertEqual('not_reported', result['usage_status'])
        self.assertIsNone(result['total_tokens'])

    def test_persistence_failure_is_explicit_even_during_stream(self):
        collector = self.collector()
        with patch('factory.usage.write_json', side_effect=OSError('synthetic disk failure')):
            with self.assertRaises(ExecutionFailure):
                self.run_stream(collector, self.emit(codex()))
        self.assertTrue(collector.persistence_failed)

    def fake_metadata_cli(self, payload):
        path = self.root / f'fake-cli-{self.number}'
        path.write_text(f'#!{sys.executable}\nimport json, sys\n'
                        'assert sys.argv[1:] == ["api", "session.get", "--param", "sessionID=session-1"]\n'
                        f'print(json.dumps({payload!r}))\n')
        path.chmod(0o700)
        return str(path)

    def test_opencode_metadata_is_scoped_to_owned_session_and_directory(self):
        valid = {'data': {'id': 'session-1', 'location': {'directory': str(self.root)},
                          'model': {'providerID': 'synthetic', 'id': 'test-model'}}}
        for change, expected in (({}, True), ({'id': 'other-session'}, False),
                                 ({'location': {'directory': str(self.root / 'other')}}, False),
                                 ({'model': {'providerID': 'synthetic', 'id': 'sk-private'}}, False)):
            with self.subTest(change=change):
                collector = self.collector('opencode')
                collector.consume(step())
                payload = {'data': dict(valid['data'], **change)}
                opencode_session_model(collector, self.fake_metadata_cli(payload), self.root, os.environ.copy())
                result = collector.snapshot()
                self.assertEqual(expected, result['reported_session_model'] is not None)
                if expected:
                    self.assertEqual('session_selection_not_per_step', result['reported_session_model']['scope'])
                    self.assertIsNone(result['groups'][0]['model'])
                self.assertNotIn('messages', collector.path.read_text())


if __name__ == '__main__':
    unittest.main()
