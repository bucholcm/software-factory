import json
import unittest

from factory.pricing import MAX_CATALOG_BYTES, estimate_cost, freeze_catalog


def synthetic_rate(**changes):
    """Synthetic test values, not published provider prices."""
    result = dict(provider='synthetic', model='test-model', currency='USD', unit_tokens=1000,
                  input_per_unit='2', cached_input_per_unit='0.5', cache_write_per_unit='3',
                  output_per_unit='8', source_url='https://example.com/synthetic-pricing',
                  effective_date='2026-01-01')
    result.update(changes)
    return result


def catalog(*rates):
    return freeze_catalog({'usage_pricing': {'version': 'synthetic-v1', 'rates': list(rates or [synthetic_rate()])}})


def group(**tokens):
    counters = dict(input_tokens=0, cached_input_tokens=0, cache_write_input_tokens=0,
                    output_tokens=0, reasoning_output_tokens=0)
    counters.update(tokens)
    return dict(provider='synthetic', model='test-model', tokens=counters)


class PricingTests(unittest.TestCase):
    def test_disjoint_cache_and_output_including_reasoning(self):
        result = estimate_cost([group(input_tokens=1000, cached_input_tokens=2000,
                                      cache_write_input_tokens=1000, output_tokens=500,
                                      reasoning_output_tokens=400)], catalog())
        self.assertEqual('estimated', result['status'])
        self.assertEqual('10.0', result['currencies'][0]['amount'])
        self.assertEqual(4, len(result['lines'][0]['components']))
        self.assertEqual('api_equivalent_estimate_not_invoice', result['kind'])

    def test_missing_counts_preserve_known_subtotal(self):
        result = estimate_cost([group(input_tokens=None, output_tokens=1000)], catalog())
        self.assertEqual('partial', result['status'])
        self.assertEqual('8', result['currencies'][0]['known_amount'])
        self.assertNotIn('amount', result['currencies'][0])
        self.assertEqual('unknown_tokens', result['lines'][0]['components'][0]['reason'])

    def test_missing_rate_is_not_zero_and_zero_counts_need_no_rate(self):
        frozen = catalog(synthetic_rate(input_per_unit=None))
        self.assertEqual('estimated', estimate_cost([group()], frozen)['status'])
        result = estimate_cost([group(input_tokens=1000)], frozen)
        self.assertEqual('partial', result['status'])
        self.assertEqual('missing_rate', result['lines'][0]['components'][0]['reason'])
        result = estimate_cost([group(input_tokens=None, cached_input_tokens=None,
                                      cache_write_input_tokens=None, output_tokens=None)], frozen)
        self.assertEqual('unpriced', result['status'])
        self.assertEqual([{'currency': 'USD', 'status': 'unpriced'}], result['currencies'])
        self.assertNotIn('known_amount', result['lines'][0])

    def test_unknown_model_only_uses_explicit_assumption(self):
        observed = group(input_tokens=1000)
        observed['model'] = None
        result = estimate_cost([observed], catalog())
        self.assertEqual('unpriced', result['status'])
        self.assertEqual([], result['currencies'])
        result = estimate_cost([observed], catalog(), {'provider': 'synthetic', 'model': 'test-model'})
        self.assertEqual('estimated', result['status'])
        self.assertEqual('configured_assumption', result['lines'][0]['basis'])
        self.assertIsNone(result['lines'][0]['model'])
        self.assertEqual('test-model', result['lines'][0]['rate']['model'])

    def test_assumption_never_reroutes_a_known_observed_identity(self):
        assumption = {'provider': 'synthetic', 'model': 'assumed-model'}
        frozen = catalog(synthetic_rate(), synthetic_rate(model='assumed-model', input_per_unit='99'))
        observed = group(input_tokens=1000)
        observed['model'] = 'observed-without-rate'
        result = estimate_cost([observed], frozen, assumption)
        self.assertEqual('unpriced', result['status'])
        self.assertEqual([], result['currencies'])
        self.assertIsNone(result['lines'][0]['rate'])
        self.assertIsNone(result['lines'][0]['basis'])
        result = estimate_cost([group(input_tokens=1000)], frozen, assumption)
        self.assertEqual('estimated', result['status'])
        self.assertEqual('observed_model', result['lines'][0]['basis'])
        self.assertEqual('test-model', result['lines'][0]['rate']['model'])
        self.assertEqual('2', result['currencies'][0]['amount'])
        observed['provider'] = None
        result = estimate_cost([observed], frozen, assumption)
        self.assertEqual('configured_assumption', result['lines'][0]['basis'])
        self.assertEqual('99', result['currencies'][0]['amount'])

    def test_mixed_currencies_are_never_added(self):
        frozen = catalog(synthetic_rate(), synthetic_rate(model='euro-model', currency='EUR'))
        euro = group(input_tokens=1000)
        euro['model'] = 'euro-model'
        result = estimate_cost([group(input_tokens=2000), euro], frozen)
        self.assertEqual([{'currency': 'EUR', 'status': 'estimated', 'amount': '2'},
                          {'currency': 'USD', 'status': 'estimated', 'amount': '4'}], result['currencies'])

    def test_catalog_and_estimates_are_detached_historical_snapshots(self):
        config = {'usage_pricing': {'version': 'synthetic-v1', 'rates': [synthetic_rate()]}}
        frozen = freeze_catalog(config)
        encoded = json.dumps(frozen)
        result = estimate_cost([group(input_tokens=1000)], frozen)
        config['usage_pricing']['rates'][0]['input_per_unit'] = '99'
        self.assertEqual(encoded, json.dumps(frozen))
        newer = freeze_catalog(config)
        self.assertNotEqual(frozen['catalog_sha256'], newer['catalog_sha256'])
        self.assertEqual('2', result['lines'][0]['amount'])
        self.assertEqual('99', estimate_cost([group(input_tokens=1000)], newer)['lines'][0]['amount'])
        frozen['rates'][0]['input_per_unit'] = '300'
        self.assertEqual('2', result['lines'][0]['rate']['input_per_unit'])
        with self.assertRaises(ValueError):
            estimate_cost([group()], frozen)

    def test_invalid_decimal_values_are_rejected(self):
        for value in (True, False, float('nan'), 0.2, 'NaN', 'Infinity', '-1', '1e10000', {}, ''):
            with self.subTest(value=value), self.assertRaises(ValueError):
                catalog(synthetic_rate(input_per_unit=value))
        for value in (True, '0', '-1', 'NaN', 3):
            with self.subTest(unit=value), self.assertRaises(ValueError):
                catalog(synthetic_rate(unit_tokens=value))

    def test_invalid_metadata_and_duplicate_models(self):
        for changes in ({'source_url': 'http://example.com'}, {'source_url': 'https://user:pw@example.com'},
                        {'effective_date': '2026-02-30'}, {'currency': 'usd'}, {'model': ''}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                catalog(synthetic_rate(**changes))
        with self.assertRaises(ValueError):
            catalog(synthetic_rate(), synthetic_rate())

    def test_safe_public_identifiers_and_assumptions(self):
        unsafe = ('has space', 'has\ncontrol', '/absolute', '../relative', 'vendor/../model',
                  'C:/absolute', 'sk-private', 'ghp_private', 'github_pat_private',
                  'vendor/sk-private', 'x' * 201)
        for field in ('provider', 'model'):
            for value in unsafe:
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    catalog(synthetic_rate(**{field: value}))
        for value in unsafe:
            with self.subTest(version=value), self.assertRaises(ValueError):
                freeze_catalog({'usage_pricing': {'version': value, 'rates': []}})
        public = catalog(synthetic_rate(model='vendor/model-1.2:public'))
        self.assertEqual('vendor/model-1.2:public', public['rates'][0]['model'])
        for assumption in ({'provider': 'synthetic', 'model': 'test-model', 'secret': 'private'},
                           {'provider': 'synthetic'}, {'provider': 'sk-private', 'model': 'test-model'}):
            with self.subTest(assumption=assumption), self.assertRaises(ValueError):
                estimate_cost([group()], public, assumption)

    def test_sources_reject_queries_userinfo_and_controls_but_allow_fragments(self):
        for source in ('https://example.com/prices?key=private', 'https://example.com/prices?',
                       'https://@example.com/prices', 'https://user@example.com/prices',
                       'https://example.com/has space', 'https://example.com/\nprices'):
            with self.subTest(source=source), self.assertRaises(ValueError):
                catalog(synthetic_rate(source_url=source))
        frozen = catalog(synthetic_rate(source_url='https://example.com/pricing#model'))
        self.assertEqual('https://example.com/pricing#model', frozen['rates'][0]['source_url'])

    def test_catalog_exact_serialized_size_boundary_includes_hash(self):
        rates = [synthetic_rate(model=f'model-{i}') for i in range(8)]
        frozen = catalog(*rates)
        size = len(json.dumps(frozen, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode())
        remaining = MAX_CATALOG_BYTES - size
        for rate in rates:
            added = min(remaining, 2048 - len(rate['source_url']))
            rate['source_url'] += 'x' * added
            remaining -= added
        self.assertEqual(0, remaining)
        frozen = catalog(*rates)
        self.assertEqual(MAX_CATALOG_BYTES, len(json.dumps(frozen, sort_keys=True, separators=(',', ':'),
                                                          ensure_ascii=True).encode()))
        # One additional metadata byte crosses the bound without changing rates.
        rates[-1]['model'] += 'x'
        with self.assertRaisesRegex(ValueError, '16 KiB'):
            catalog(*rates)

    def test_absent_catalog_and_unknown_token_values(self):
        self.assertIsNone(freeze_catalog({}))
        self.assertEqual('unpriced', estimate_cost([group(input_tokens=1)], None)['status'])
        self.assertEqual('estimated', estimate_cost([group()], None)['status'])
        self.assertEqual('unpriced', estimate_cost([], None)['status'])
        for value in (True, -1, '1', 1.5):
            with self.subTest(value=value):
                result = estimate_cost([group(input_tokens=value)], catalog())
                self.assertEqual('partial', result['status'])
                self.assertIsNone(result['lines'][0]['components'][0]['tokens'])

    def test_decimal_precision_and_json_roundtrip(self):
        frozen = catalog(synthetic_rate(input_per_unit='0.000000000000000001'))
        result = estimate_cost([group(input_tokens=1)], json.loads(json.dumps(frozen)))
        self.assertEqual('0.000000000000000000001', result['currencies'][0]['amount'])
        self.assertEqual(result, json.loads(json.dumps(result)))
        self.assertEqual(frozen['catalog_sha256'], catalog(synthetic_rate(input_per_unit='0.000000000000000001'))['catalog_sha256'])


if __name__ == '__main__':
    unittest.main()
