"""Reports from synthetic local stores and real per-attempt usage journals."""
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest

from factory.pricing import freeze_catalog
from factory.store import Store
from factory.usage import Collector
from factory.usage_report import cost_totals, project_report, report, summarize


def pricing(value='2', currency='USD', version='synthetic-v1', unit=1000):
    return {'usage_pricing': {'version': version, 'rates': [
        {'provider': 'synthetic', 'model': 'test-model', 'currency': currency, 'unit_tokens': unit,
         'input_per_unit': value, 'cached_input_per_unit': value, 'cache_write_per_unit': value,
         'output_per_unit': value, 'source_url': 'https://example.com/synthetic-pricing',
         'effective_date': '2026-01-01'}]},
        'usage_price_assumptions': {'codex': {'provider': 'synthetic', 'model': 'test-model'}}}


class UsageReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / 'state'
        self.store = Store(self.state)
        self.job = self.store.enqueue('synthetic-task', self.root, 'synthetic-branch')
        self.sequence = 0

    def attempt(self, job=None, stage='implementing', config=None, status='completed', tokens=None, runtime='codex'):
        job = dict(job or self.job, stage=stage)
        self.sequence += 1
        directory = self.state / 'jobs' / job['id'] / f'{stage}-{self.sequence}-100'
        directory.mkdir(parents=True)
        collector = Collector(job, runtime, directory / 'usage.json', config or pricing())
        counters = dict(input_tokens=100, cached_input_tokens=20, cache_write_input_tokens=10,
                        output_tokens=30, reasoning_output_tokens=5)
        if tokens is not None:
            counters.update(tokens)
        if runtime == 'codex':
            collector.consume({'type': 'turn.completed', 'usage': counters})
        else:
            collector.consume({'type': 'step_finish', 'sessionID': 'session-1', 'part': {
                'id': 'part-1', 'sessionID': 'session-1', 'messageID': 'message-1', 'reason': 'stop',
                'tokens': {'input': counters['input_tokens'],
                           'output': counters['output_tokens'] - counters['reasoning_output_tokens'],
                           'reasoning': counters['reasoning_output_tokens'],
                           'cache': {'read': counters['cached_input_tokens'],
                                     'write': counters['cache_write_input_tokens']}}}})
        collector.finish(status, 0 if status == 'completed' else 1)
        self.store.event(job['id'], stage, 'worker_started')
        return collector.path

    def mutate(self, path, **changes):
        raw = json.loads(path.read_text())
        raw.update(changes)
        path.write_text(json.dumps(raw))

    def test_repeat_read_and_store_restart_do_not_double_count(self):
        self.attempt()
        first = project_report(self.state)
        Store(self.state)
        second = project_report(self.state)
        self.assertEqual(first, second)
        self.assertEqual(1, second['summary']['attempts'])
        self.assertEqual(130, second['summary']['total_tokens'])
        self.assertEqual('0.26', second['summary']['estimated_cost']['currencies'][0]['amount'])
        self.assertEqual(second['summary'], summarize(second['jobs'][0]['attempts']))
        self.assertEqual(second['summary']['estimated_cost'], cost_totals(second['jobs'][0]['attempts']))

    def test_old_spec_and_worker_event_are_one_missing_attempt_not_zero(self):
        directory = self.state / 'jobs' / self.job['id'] / 'implementing-1-100'
        directory.mkdir(parents=True)
        (directory / 'spec.json').write_text('{"private": "PRIVATE_SPEC_MARKER"}')
        self.store.event(self.job['id'], 'implementing', 'worker_started')
        result = project_report(self.state)
        summary = result['summary']
        self.assertEqual(1, summary['missing_attempts'])
        self.assertEqual(1, summary['attempts'])
        self.assertEqual(0, summary['recorded_attempts'])
        self.assertIsNone(summary['total_tokens'])
        self.assertIsNone(summary['known_total_tokens'])
        self.assertEqual('not_recorded', summary['coverage'])
        self.assertEqual('unpriced', summary['estimated_cost']['status'])
        self.assertNotIn('PRIVATE_SPEC_MARKER', json.dumps(result))

    def test_stage_job_and_project_totals_include_failed_retries(self):
        self.attempt(status='failed')
        self.attempt()
        self.attempt(stage='reviewing')
        other = self.store.enqueue('other-task', self.root, 'other-branch')
        self.attempt(job=other)
        result = project_report(self.state)
        self.assertEqual(4, result['summary']['recorded_attempts'])
        self.assertEqual(520, result['summary']['total_tokens'])
        self.assertEqual('partial', result['summary']['coverage'])
        selected = project_report(self.state, self.job['task_id'])
        self.assertEqual(3, selected['summary']['recorded_attempts'])
        self.assertEqual(390, selected['summary']['total_tokens'])
        stages = {row['stage']: row for row in selected['jobs'][0]['stages']}
        self.assertEqual(260, stages['implementing']['total_tokens'])
        self.assertEqual(130, stages['reviewing']['total_tokens'])
        self.assertEqual('failed', selected['jobs'][0]['attempts'][0]['execution_status'])
        self.assertEqual(selected, project_report(self.state, self.job['id']))

    def test_missing_components_preserve_known_evidence_without_zero(self):
        config = pricing()
        config['usage_price_assumptions']['opencode'] = {'provider': 'synthetic', 'model': 'test-model'}
        self.attempt(tokens={'input_tokens': None, 'cached_input_tokens': None}, runtime='opencode', config=config)
        result = project_report(self.state)
        summary = result['summary']
        self.assertEqual('partial', summary['coverage'])
        self.assertIsNone(summary['tokens']['input_tokens'])
        self.assertIsNone(summary['tokens']['cached_input_tokens'])
        self.assertIsNone(summary['total_tokens'])
        self.assertEqual(40, summary['known_total_tokens'])
        self.assertEqual('0.08', summary['estimated_cost']['currencies'][0]['known_amount'])
        self.assertNotIn('amount', summary['estimated_cost']['currencies'][0])

    def test_snapshot_prices_are_historical_reprice_is_separate_and_read_only(self):
        original_config = pricing()
        path = self.attempt(config=original_config)
        before = path.read_bytes()
        original_config['usage_pricing']['rates'][0]['input_per_unit'] = '900'
        original_config['usage_pricing']['version'] = 'changed-config'
        current = dict(original_config, state_dir=str(self.state))
        historical = report(current)['projects'][0]['jobs'][0]['attempts'][0]
        self.assertEqual('0.26', historical['estimated_cost']['currencies'][0]['amount'])
        new_catalog = freeze_catalog(pricing('4', version='synthetic-v2'))
        updated = report(current, reprice=new_catalog)['projects'][0]['jobs'][0]['attempts'][0]
        self.assertEqual(historical['estimated_cost'], updated['estimated_cost'])
        self.assertEqual('0.52', updated['recalculated_estimate']['currencies'][0]['amount'])
        self.assertEqual('synthetic-v2', updated['recalculated_estimate']['catalog_version'])
        self.assertEqual(before, path.read_bytes())

    def test_explicit_new_model_scenario_never_relabels_or_overwrites_history(self):
        path = self.attempt(config={'usage_pricing': pricing()['usage_pricing']})
        before = path.read_bytes()
        original = project_report(self.state)
        self.assertEqual('unpriced', original['summary']['estimated_cost']['status'])
        value = report({'state_dir': str(self.state)}, reprice_assumptions={'codex': {'provider': 'synthetic', 'model': 'test-model'}})
        self.assertEqual('0.26', value['summary']['recalculated_estimate']['currencies'][0]['amount'])
        self.assertEqual('unpriced', value['summary']['estimated_cost']['status'])
        self.assertIsNone(value['projects'][0]['jobs'][0]['attempts'][0]['groups'][0]['model'])
        self.assertEqual(before, path.read_bytes())

    def test_mixed_currencies_stay_separate(self):
        self.attempt(config=pricing(currency='EUR'))
        self.attempt(config=pricing(currency='USD'))
        rows = project_report(self.state)['summary']['estimated_cost']['currencies']
        self.assertEqual(['EUR', 'USD'], [row['currency'] for row in rows])
        self.assertEqual(['0.26', '0.26'], [row['amount'] for row in rows])

    def test_large_valid_derived_decimal_amount_is_not_rejected_as_rate(self):
        self.attempt(config=pricing('1e100', unit=1),
                     tokens={'input_tokens': 10**15, 'cached_input_tokens': 0,
                             'cache_write_input_tokens': 0, 'output_tokens': 0, 'reasoning_output_tokens': 0})
        result = project_report(self.state)
        self.assertEqual(0, result['jobs'][0]['invalid_attempts'])
        amount = result['summary']['estimated_cost']['currencies'][0]['amount']
        self.assertEqual(Decimal('1e115'), Decimal(amount))

    def test_related_aliases_are_deduplicated_and_unavailable_source_is_partial(self):
        self.attempt()
        alias = self.root / 'state-alias'
        alias.symlink_to(self.state, target_is_directory=True)
        config = {'state_dir': str(self.state), 'reliability_sources': [str(alias), str(self.state / '.'),
                                                                    str(self.root / 'unavailable')]}
        result = report(config, related=True)
        self.assertEqual(2, len(result['projects']))
        self.assertEqual(1, result['unavailable_sources'])
        self.assertEqual(1, result['summary']['recorded_attempts'])
        self.assertEqual(130, result['summary']['known_total_tokens'])
        self.assertIsNone(result['summary']['total_tokens'])
        self.assertEqual('partial', result['summary']['coverage'])
        self.assertEqual('0.26', result['summary']['estimated_cost']['currencies'][0]['known_amount'])
        self.assertEqual(1, len(report(config)['projects']))

    def test_allowlist_ignores_private_extras_and_recomputes_derived_fields(self):
        path = self.attempt()
        raw = json.loads(path.read_text())
        raw['groups'][0]['private_prompt'] = 'PRIVATE_GROUP_MARKER'
        raw['groups'][0]['tokens']['private'] = 'PRIVATE_TOKEN_MARKER'
        raw['estimated_cost'] = {'private': 'PRIVATE_ESTIMATE_MARKER'}
        raw['private_stdout'] = 'PRIVATE_STDOUT_MARKER'
        raw['known_tokens'] = 999999
        path.write_text(json.dumps(raw))
        result = project_report(self.state)
        self.assertNotIn('PRIVATE_', json.dumps(result))
        self.assertEqual(130, result['summary']['known_total_tokens'])
        self.assertEqual('0.26', result['summary']['estimated_cost']['currencies'][0]['amount'])

    def test_wrong_bindings_invalid_private_models_and_symlink_records_are_rejected(self):
        changes = ({'job_id': 'another-job'}, {'attempt_id': 'implementing-999-100'},
                   {'requested_model': 'sk-PRIVATE_MODEL_MARKER'})
        for change in changes:
            path = self.attempt()
            self.mutate(path, **change)
        path = self.attempt()
        target = self.root / 'external-record.json'
        path.rename(target)
        path.symlink_to(target)
        result = project_report(self.state)
        self.assertEqual(4, result['jobs'][0]['invalid_attempts'])
        self.assertEqual(4, result['summary']['missing_attempts'])
        self.assertEqual(0, result['summary']['recorded_attempts'])
        self.assertIsNone(result['summary']['total_tokens'])
        self.assertNotIn('PRIVATE_', json.dumps(result))

    def test_origin_contains_only_payload_hashes_never_requirements(self):
        origin = {'incident_signature': 'a' * 64, 'project_key': 'b' * 16,
                  'requirements': 'PRIVATE_ORIGIN_MARKER'}
        self.store.update(self.job['id'], payload={'origin': origin, 'requirements': 'PRIVATE_REQUIREMENTS_MARKER'})
        self.attempt()
        result = project_report(self.state)
        self.assertEqual({'incident_signature': 'a' * 64, 'project_key': 'b' * 16}, result['jobs'][0]['origin'])
        self.assertNotIn('PRIVATE_', json.dumps(result))
        self.store.update(self.job['id'], payload={'origin': {'incident_signature': 'not-a-hash', 'project_key': 'b' * 16}})
        self.assertIsNone(project_report(self.state)['jobs'][0]['origin'])


if __name__ == '__main__':
    unittest.main()
