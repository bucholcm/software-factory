"""Idempotent attempt/job/stage reports from allowlisted usage journals."""
from __future__ import annotations

from collections import defaultdict
from contextlib import closing
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3

from .pricing import estimate_cost, freeze_catalog
from .usage import BILLABLE_KEYS, TOKEN_KEYS, MAX_RECORD, counter, identifier, totals

AGENT_STAGES = {'implementing', 'fixing', 'reviewing'}
ATTEMPT = re.compile(r'(implementing|testing|reviewing|fixing)-[0-9]+-[0-9]+')


def money(value):
    if not isinstance(value, str) or len(value) > 512:
        raise ValueError('invalid_usage_cost')
    try:
        amount = Decimal(value)
    except InvalidOperation:
        raise ValueError('invalid_usage_cost') from None
    if not amount.is_finite() or amount < 0 or abs(amount.as_tuple().exponent) > 500 or amount.adjusted() > 500:
        raise ValueError('invalid_usage_cost')
    return amount


def when(value, optional=False):
    if optional and value is None:
        return None
    if type(value) not in {int, float} or not math.isfinite(value) or not 0 < value < 10**12:
        raise ValueError('invalid_usage_time')
    return value


def estimate_with_coverage(groups, catalog, assumption, coverage):
    estimate = estimate_cost(groups, catalog, assumption)
    estimate['coverage'] = coverage
    if coverage != 'complete' and estimate['status'] == 'estimated':
        estimate['status'] = 'partial'
        for row in estimate['currencies']:
            row['status'] = 'partial'
            if 'amount' in row:
                row['known_amount'] = row.pop('amount')
    return estimate


def read_record(path, job, stage, reprice=None, reprice_assumptions=None):
    if path.is_symlink() or path.stat().st_size > MAX_RECORD:
        raise ValueError('invalid_usage_record')
    raw = json.loads(path.read_text())
    if not isinstance(raw, dict) or type(raw.get('schema_version')) is not int or raw['schema_version'] != 1:
        raise ValueError('invalid_usage_record')
    if any(raw.get(key) != value for key, value in {'job_id': job['id'], 'task_id': job['task_id'],
                                                  'stage': stage, 'attempt_id': path.parent.name}.items()):
        raise ValueError('usage_binding_mismatch')
    if raw.get('runtime') not in {'codex', 'opencode'}:
        raise ValueError('invalid_usage_runtime')
    execution = raw.get('execution_status')
    if execution not in {'running', 'completed', 'failed', 'timeout', 'not_started'}:
        raise ValueError('invalid_usage_execution')
    groups = raw.get('groups')
    if not isinstance(groups, list) or len(groups) > 16:
        raise ValueError('invalid_usage_groups')
    safe_groups = []
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get('tokens'), dict):
            raise ValueError('invalid_usage_tokens')
        safe = {'provider': None, 'model': None, 'tokens': {}}
        for key in ('provider', 'model'):
            value = group.get(key)
            if value is not None and identifier(value) is None:
                raise ValueError('invalid_usage_model')
            safe[key] = value
        for key in TOKEN_KEYS:
            value = group['tokens'].get(key)
            if value is not None and counter(value) is None:
                raise ValueError('invalid_usage_counter')
            safe['tokens'][key] = value
        safe_groups.append(safe)
    parser = raw.get('parser')
    if not isinstance(parser, dict) or any(type(parser.get(k)) is not bool for k in ('incomplete', 'terminal_event_seen')):
        raise ValueError('invalid_usage_parser')
    for key in ('invalid_events', 'oversized_events', 'open_steps'):
        if counter(parser.get(key)) is None:
            raise ValueError('invalid_usage_parser')
    token_totals = totals(safe_groups)
    all_known = bool(safe_groups) and all(token_totals[k] is not None for k in BILLABLE_KEYS)
    complete = execution == 'completed' and parser['terminal_event_seen'] and not any(parser[k] for k in parser if k != 'terminal_event_seen')
    coverage = 'complete' if complete and all_known else 'partial' if safe_groups else 'not_reported'
    if raw.get('usage_status') != coverage:
        raise ValueError('usage_coverage_mismatch')
    requested = raw.get('requested_model')
    if requested is not None and (not isinstance(requested, str) or not all(identifier(part) for part in requested.split('/'))):
        raise ValueError('invalid_requested_model')
    model = raw.get('reported_session_model')
    if model is not None:
        if not isinstance(model, dict) or not identifier(model.get('provider')) or not identifier(model.get('model')):
            raise ValueError('invalid_reported_model')
        model = {'provider': model['provider'], 'model': model['model'],
                 'source': 'opencode_session.get', 'scope': 'session_selection_not_per_step'}
    catalog = raw.get('pricing_catalog')
    frozen = freeze_catalog({'usage_pricing': catalog}) if catalog is not None else None
    if catalog is not None and catalog.get('catalog_sha256') != frozen['catalog_sha256']:
        raise ValueError('invalid_usage_catalog')
    assumption = raw.get('pricing_assumption')
    if assumption is not None:
        if not isinstance(assumption, dict) or set(assumption) != {'provider', 'model'}:
            raise ValueError('invalid_usage_assumption')
    estimate = estimate_with_coverage(safe_groups, frozen, assumption, coverage)
    reported = raw.get('runtime_reported_cost')
    if reported is not None:
        if not isinstance(reported, dict) or reported.get('currency') != 'USD' or reported.get('coverage') not in {'complete', 'partial'}:
            raise ValueError('invalid_runtime_cost')
        cost_key = 'amount' if reported['coverage'] == 'complete' else 'known_amount'
        reported = {'currency': 'USD', cost_key: format(money(reported.get(cost_key)), 'f'),
                    'coverage': reported['coverage'], 'kind': 'runtime_reported_not_invoice'}
    total = raw.get('total_tokens')
    inclusive_input = raw.get('reported_input_total_tokens')
    if any(value is not None and counter(value) is None for value in (total, inclusive_input)):
        raise ValueError('invalid_usage_total')
    if all_known and total != sum(token_totals[k] for k in BILLABLE_KEYS):
        raise ValueError('usage_total_mismatch')
    known_values = [g['tokens'][k] for g in safe_groups for k in BILLABLE_KEYS if g['tokens'][k] is not None]
    result = {'attempt_id': raw['attempt_id'], 'stage': stage, 'runtime': raw['runtime'],
              'requested_model': requested, 'reported_session_model': model,
              'runtime_session_id': identifier(raw.get('session_id')),
              'model_source': 'runtime_session_selection' if model else 'not_reported',
              'started_at': when(raw.get('started_at')), 'updated_at': when(raw.get('updated_at')),
              'finished_at': when(raw.get('finished_at'), optional=True),
              'execution_status': execution, 'usage_status': coverage,
              'tokens': token_totals, 'total_tokens': total,
              'reported_input_total_tokens': inclusive_input,
              'known_tokens': sum(known_values) if known_values else None,
              'groups': safe_groups, 'runtime_reported_cost': reported, 'estimated_cost': estimate}
    if execution == 'running' and (job['stage'] not in AGENT_STAGES or Path(job.get('attempt_dir') or '').name != path.parent.name):
        result['execution_status'] = 'interrupted'
        result['usage_status'] = 'partial' if safe_groups else 'not_reported'
    if reprice is not None or reprice_assumptions is not None:
        new_assumption = (reprice_assumptions or {}).get(raw['runtime'], assumption)
        result['recalculated_estimate'] = estimate_with_coverage(safe_groups, reprice or frozen, new_assumption, result['usage_status'])
    return result


def cost_totals(attempts, missing=0):
    complete = bool(attempts) and missing == 0 and all(a['estimated_cost']['status'] == 'estimated' for a in attempts)
    sums = {}
    versions = set()
    assumed_models = set()
    for attempt in attempts:
        estimate = attempt['estimated_cost']
        for line in estimate.get('lines', []):
            if line.get('basis') == 'configured_assumption' and line.get('rate'):
                assumed_models.add((line['rate']['provider'], line['rate']['model']))
        if estimate.get('catalog_version'):
            versions.add(estimate['catalog_version'])
        for row in estimate.get('currencies', []):
            value = row.get('amount', row.get('known_amount'))
            if value is not None:
                with localcontext() as context:
                    context.prec = 1000
                    sums[row['currency']] = sums.get(row['currency'], Decimal(0)) + money(value)
    return {'kind': 'api_equivalent_estimate_not_invoice',
            'status': 'estimated' if complete else 'partial' if sums else 'unpriced',
            'currencies': [{'currency': currency, 'status': 'estimated' if complete else 'partial',
                            'amount' if complete else 'known_amount': format(value, 'f')} for currency, value in sorted(sums.items())],
            'catalog_versions': sorted(versions),
            'assumed_models': [{'provider': p, 'model': m} for p, m in sorted(assumed_models)]}


def summarize(attempts, missing=0):
    complete = bool(attempts) and not missing and all(a['usage_status'] == 'complete' for a in attempts)
    known = [a['total_tokens'] if a['total_tokens'] is not None else a['known_tokens'] for a in attempts]
    known = [n for n in known if n is not None]
    totals_by_key = {}
    for key in TOKEN_KEYS:
        values = [a['tokens'][key] for a in attempts]
        totals_by_key[key] = sum(values) if values and not missing and all(v is not None for v in values) else None
    total_values = [a['total_tokens'] for a in attempts]
    input_values = [a['reported_input_total_tokens'] for a in attempts]
    models = {}
    for attempt in attempts:
        model = {'runtime': attempt['runtime'], 'requested_model': attempt['requested_model'],
                 'reported_session_model': attempt['reported_session_model'], 'model_source': attempt['model_source']}
        models[json.dumps(model, sort_keys=True)] = model
    reported = [a['runtime_reported_cost'] for a in attempts if a['runtime_reported_cost'] is not None]
    with localcontext() as context:
        context.prec = 1000
        runtime_cost = sum((money(a.get('amount', a.get('known_amount'))) for a in reported), Decimal(0))
    reported_complete = len(reported) == len(attempts) and not missing and all(r['coverage'] == 'complete' for r in reported)
    result = {'coverage': 'complete' if complete else 'partial' if known else 'not_recorded',
            'attempts': len(attempts) + missing, 'recorded_attempts': len(attempts), 'missing_attempts': missing,
            'tokens': totals_by_key,
            'total_tokens': sum(total_values) if total_values and not missing and all(v is not None for v in total_values) else None,
            'known_total_tokens': sum(known) if known else None,
            'reported_input_total_tokens': sum(input_values) if input_values and not missing and all(v is not None for v in input_values) else None,
            'models': list(models.values()), 'estimated_cost': cost_totals(attempts, missing),
            'runtime_reported_cost': {'currency': 'USD', 'amount' if reported_complete else 'known_amount': format(runtime_cost, 'f'),
                                      'coverage': 'complete' if reported_complete else 'partial',
                                      'kind': 'runtime_reported_not_invoice'} if reported else None}
    if any('recalculated_estimate' in attempt for attempt in attempts):
        recalculated = [{**a, 'estimated_cost': a.get('recalculated_estimate', a['estimated_cost'])} for a in attempts]
        result['recalculated_estimate'] = cost_totals(recalculated, missing)
    return result


def mark_incomplete(summary):
    """Keep known subtotals without publishing them as complete global totals."""
    summary['coverage'] = 'partial' if summary['known_total_tokens'] is not None else 'unavailable'
    summary['total_tokens'] = None
    summary['reported_input_total_tokens'] = None
    summary['tokens'] = {key: None for key in TOKEN_KEYS}
    for field in ('estimated_cost', 'recalculated_estimate'):
        estimate = summary.get(field)
        if estimate and estimate['status'] == 'estimated':
            estimate['status'] = 'partial'
            for row in estimate['currencies']:
                row['status'] = 'partial'
                if 'amount' in row:
                    row['known_amount'] = row.pop('amount')
    runtime = summary.get('runtime_reported_cost')
    if runtime:
        runtime['coverage'] = 'partial'
        if 'amount' in runtime:
            runtime['known_amount'] = runtime.pop('amount')


def project_report(state_dir, task_id=None, reprice=None, reprice_assumptions=None):
    state = Path(state_dir).expanduser().resolve()
    database = state / 'execution.sqlite3'
    if not database.exists():
        return {'status': 'unavailable', 'jobs': [], 'summary': summarize([])}
    try:
        with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as db:
            db.row_factory = sqlite3.Row
            jobs = [dict(row) for row in db.execute("""SELECT id,task_id,stage,attempt_dir,
                json_extract(payload,'$.origin') AS origin FROM jobs ORDER BY created_at,id""")]
            expected = {(row['job_id'], row['stage']): row['n'] for row in db.execute("""SELECT job_id,stage,count(*) AS n
                FROM events WHERE code='worker_started' AND stage IN ('implementing','fixing','reviewing') GROUP BY job_id,stage""")}
    except (OSError, sqlite3.Error):
        return {'status': 'unavailable', 'jobs': [], 'summary': summarize([])}
    result, all_attempts, all_missing, invalid_jobs = [], [], 0, 0
    for job in jobs:
        if task_id is not None and task_id not in {job['id'], job['task_id']}:
            continue
        if not identifier(job['id']) or not identifier(job['task_id']):
            invalid_jobs += 1
            continue
        directory = state / 'jobs' / job['id']
        attempts, missing, invalid = [], 0, 0
        missing_by_stage = defaultdict(int)
        if directory.is_dir() and not directory.is_symlink() and not (state / 'jobs').is_symlink():
            for folder in sorted(directory.iterdir()):
                match = ATTEMPT.fullmatch(folder.name)
                if not match or match[1] not in AGENT_STAGES:
                    continue
                if folder.is_symlink() or not folder.is_dir():
                    missing_by_stage[match[1]] += 1
                    invalid += 1
                    continue
                path = folder / 'usage.json'
                if not path.exists():
                    if (folder / 'spec.json').exists():
                        missing_by_stage[match[1]] += 1
                    continue
                try:
                    attempts.append(read_record(path, job, match[1], reprice, reprice_assumptions))
                except (OSError, ValueError, TypeError, KeyError, RecursionError):
                    missing_by_stage[match[1]] += 1
                    invalid += 1
        for stage in AGENT_STAGES:
            observed = sum(a['stage'] == stage for a in attempts)
            missing_by_stage[stage] = max(missing_by_stage[stage], expected.get((job['id'], stage), 0) - observed)
        missing = sum(missing_by_stage.values())
        origin = None
        if isinstance(job.get('origin'), str):
            try:
                raw = json.loads(job['origin'])
                if isinstance(raw, dict) and re.fullmatch(r'[0-9a-f]{64}', raw.get('incident_signature', '')) and re.fullmatch(r'[0-9a-f]{16}', raw.get('project_key', '')):
                    origin = {'incident_signature': raw['incident_signature'], 'project_key': raw['project_key']}
            except (ValueError, TypeError):
                pass
        stages = []
        for stage in sorted(AGENT_STAGES):
            subset = [a for a in attempts if a['stage'] == stage]
            if subset or missing_by_stage[stage]:
                stages.append({'stage': stage, **summarize(subset, missing_by_stage[stage])})
        result.append({'job_id': job['id'], 'task_id': job['task_id'], 'origin': origin,
                       'invalid_attempts': invalid, 'summary': summarize(attempts, missing),
                       'stages': stages, 'attempts': attempts})
        all_attempts.extend(attempts)
        all_missing += missing
    summary = summarize(all_attempts, all_missing)
    if invalid_jobs:
        mark_incomplete(summary)
    return {'status': 'incomplete' if invalid_jobs else 'ok', 'invalid_jobs': invalid_jobs,
            'project': state.name, 'project_key': hashlib.sha256(str(state).encode()).hexdigest()[:16],
            'jobs': result, 'summary': summary}


def report(config, task_id=None, related=False, reprice=None, reprice_assumptions=None):
    paths = [config['state_dir'], *(config.get('reliability_sources', []) if related else [])]
    projects = [project_report(path, task_id, reprice, reprice_assumptions) for path in sorted({str(Path(p).expanduser().resolve()) for p in paths})]
    attempts = [attempt for project in projects for job in project['jobs'] for attempt in job['attempts']]
    missing = sum(job['summary']['missing_attempts'] for project in projects for job in project['jobs'])
    summary = summarize(attempts, missing)
    unavailable = sum(project['status'] != 'ok' for project in projects)
    if unavailable:
        mark_incomplete(summary)
    return {'projects': projects, 'summary': summary, 'unavailable_sources': unavailable}
