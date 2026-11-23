"""Bounded JSONL reduction: retain usage metadata, never runtime transcripts."""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, localcontext
import hashlib
import json
import math
import os
from pathlib import Path
import re
import select
import subprocess
import threading
import time

from .config import write_json
from .environment import ExecutionFailure
from .pricing import estimate_cost, freeze_catalog

TOKEN_KEYS = ('input_tokens', 'cached_input_tokens', 'cache_write_input_tokens', 'output_tokens', 'reasoning_output_tokens')
BILLABLE_KEYS = TOKEN_KEYS[:4]
MAX_EVENT = 1024 * 1024
MAX_RECORD = 65536
MAX_STEPS = 10000


def counter(value):
    return value if type(value) is int and 0 <= value <= 10**15 else None


def identifier(value):
    if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:+/-]{0,159}', value):
        if not value.startswith(('sk-', 'ghp_', 'github_pat_')) and '/' not in value:
            return value
    return None


def totals(groups):
    result = {}
    for key in TOKEN_KEYS:
        values = [group['tokens'].get(key) for group in groups]
        result[key] = sum(values) if values and all(v is not None for v in values) else None
    return result


class Collector:
    def __init__(self, job, runtime, path, config, requested_model=None):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.runtime = runtime
        self.job = job
        self.requested_model = requested_model
        self.started_at = time.time()
        self.finished_at = None
        self.execution_status = 'running'
        self.exit_code = None
        self.session_id = None
        self.groups = {}
        self.units = {}
        self.last_usage = None
        self.invalid_events = 0
        self.oversized_events = 0
        self.incomplete = False
        self.stream_complete = False
        self.open_steps = set()
        self.persistence_failed = False
        self.reported_session_model = None
        self.catalog = freeze_catalog(config)
        assumption = config.get('usage_price_assumptions', {}).get(runtime)
        self.assumption = {key: assumption[key] for key in ('provider', 'model')} if assumption is not None else None
        self.response = bytearray()
        self.response_overflow = False
        self.flush()

    def snapshot(self):
        groups = list(self.groups.values())
        token_totals = totals(groups)
        known_values = [group['tokens'][key] for group in groups for key in BILLABLE_KEYS if group['tokens'].get(key) is not None]
        known = sum(known_values) if known_values else None
        all_known = bool(groups) and all(token_totals[key] is not None for key in BILLABLE_KEYS)
        complete = (self.execution_status == 'completed' and self.stream_complete and not self.open_steps
                    and not self.incomplete and not self.invalid_events and not self.oversized_events)
        usage_status = 'complete' if complete and all_known else 'partial' if groups else 'not_reported'
        estimate = estimate_cost(groups, self.catalog, self.assumption)
        estimate['coverage'] = usage_status
        if usage_status != 'complete' and estimate['status'] == 'estimated':
            estimate['status'] = 'partial'
            for row in estimate['currencies']:
                row['status'] = 'partial'
                if 'amount' in row:
                    row['known_amount'] = row.pop('amount')
        total = known if all_known else None
        reported_input = sum(token_totals[key] for key in BILLABLE_KEYS[:3]) if all(token_totals[key] is not None for key in BILLABLE_KEYS[:3]) else None
        if self.runtime == 'codex' and self.last_usage:
            raw_input, raw_output = self.last_usage['input_tokens'], self.last_usage['output_tokens']
            reported_input = raw_input
            if raw_input is not None and raw_output is not None:
                total = raw_input + raw_output
        reported_costs = [unit.get('reported_cost') for unit in self.units.values()]
        reported = None
        known_costs = [value for value in reported_costs if value is not None]
        if known_costs:
            cost_complete = usage_status == 'complete' and len(known_costs) == len(reported_costs)
            with localcontext() as context:
                context.prec = 1000
                amount = sum(known_costs, Decimal(0))
            reported = {'currency': 'USD', 'amount' if cost_complete else 'known_amount': str(amount),
                        'coverage': 'complete' if cost_complete else 'partial', 'kind': 'runtime_reported_not_invoice'}
        return {
            'schema_version': 1, 'job_id': self.job['id'], 'task_id': self.job['task_id'],
            'stage': self.job['stage'], 'attempt_id': self.path.parent.name,
            'runtime': self.runtime, 'requested_model': self.requested_model,
            'reported_session_model': self.reported_session_model,
            'model_source': 'not_reported' if self.reported_session_model is None else 'runtime_session_selection',
            'session_id': self.session_id,
            'started_at': self.started_at, 'updated_at': time.time(), 'finished_at': self.finished_at,
            'execution_status': self.execution_status, 'exit_code': self.exit_code,
            'usage_status': usage_status, 'tokens': token_totals,
            'total_tokens': total, 'known_tokens': known,
            'reported_input_total_tokens': reported_input,
            'groups': groups, 'runtime_reported_cost': reported,
            'pricing_catalog': self.catalog, 'pricing_assumption': self.assumption,
            'estimated_cost': estimate,
            'parser': {'invalid_events': self.invalid_events, 'oversized_events': self.oversized_events,
                       'incomplete': self.incomplete, 'open_steps': len(self.open_steps),
                       'terminal_event_seen': self.stream_complete},
        }

    def flush(self):
        with self.lock:
            try:
                value = self.snapshot()
                if len(json.dumps(value)) > MAX_RECORD:
                    raise ValueError('usage_record_too_large')
                write_json(self.path, value)
            except (OSError, ValueError):
                self.persistence_failed = True
                raise ExecutionFailure('environment_telemetry_unavailable', phase='agent') from None

    def _rebuild_opencode(self):
        # CLI v2 drops per-message model/provider: never invent those identities.
        groups = {}
        for unit in self.units.values():
            mask = tuple(unit['tokens'][key] is not None for key in BILLABLE_KEYS)
            if mask not in groups:
                groups[mask] = {'provider': None, 'model': None, 'tokens': unit['tokens'].copy()}
            else:
                for key in TOKEN_KEYS:
                    previous, current = groups[mask]['tokens'][key], unit['tokens'][key]
                    groups[mask]['tokens'][key] = previous + current if previous is not None and current is not None else None
        self.groups = groups

    def consume(self, event):
        with self.lock:
            if not isinstance(event, dict):
                self.invalid_events += 1
                return
            kind = event.get('type')
            if self.runtime == 'codex':
                if kind == 'thread.started':
                    session = identifier(event.get('thread_id'))
                    if self.session_id and session != self.session_id:
                        self.incomplete = True
                    elif session:
                        self.session_id = session
                elif kind == 'turn.completed':
                    usage = event.get('usage')
                    if not isinstance(usage, dict):
                        self.invalid_events += 1
                        return
                    counts = {key: counter(usage.get(key)) for key in TOKEN_KEYS}
                    if self.last_usage and any(counts[k] is not None and self.last_usage[k] is not None and counts[k] < self.last_usage[k] for k in TOKEN_KEYS):
                        self.invalid_events += 1
                        return
                    self.last_usage = counts.copy()
                    input_total, cached, writes = (counts[k] for k in BILLABLE_KEYS[:3])
                    if input_total is not None and cached is not None and writes is not None:
                        fresh = input_total - cached - writes
                        counts['input_tokens'] = fresh if fresh >= 0 else None
                        if fresh < 0:
                            self.invalid_events += 1
                    else:
                        counts['input_tokens'] = None
                    if counts['reasoning_output_tokens'] is not None and counts['output_tokens'] is not None and counts['reasoning_output_tokens'] > counts['output_tokens']:
                        counts['output_tokens'] = None
                        self.invalid_events += 1
                    self.groups = {'thread': {'provider': None, 'model': None, 'tokens': counts}}
                    self.stream_complete = True
                    self.flush()
            elif self.runtime == 'opencode':
                session = identifier(event.get('sessionID'))
                if session:
                    if self.session_id and self.session_id != session:
                        self.incomplete = True
                        return
                    self.session_id = session
                part = event.get('part')
                if not isinstance(part, dict):
                    return
                if kind == 'text' and isinstance(part.get('text'), str):
                    text = part['text'].encode()
                    remaining = MAX_RECORD - len(self.response)
                    self.response.extend(text[:max(0, remaining)])
                    self.response_overflow |= len(text) > remaining
                elif kind == 'step_start':
                    message = identifier(part.get('messageID'))
                    if session and message:
                        if len(self.open_steps) < MAX_STEPS or (session, message) in self.open_steps:
                            self.open_steps.add((session, message))
                        else:
                            self.incomplete = True
                elif kind == 'step_finish':
                    message = identifier(part.get('messageID'))
                    if not session or not message or part.get('sessionID') != session:
                        self.invalid_events += 1
                        return
                    key = hashlib.sha256((session + ':' + message).encode()).hexdigest()
                    if len(self.units) >= MAX_STEPS and key not in self.units:
                        self.incomplete = True
                        return
                    usage = part.get('tokens')
                    if not isinstance(usage, dict):
                        self.invalid_events += 1
                        return
                    cache = usage.get('cache') if isinstance(usage.get('cache'), dict) else {}
                    visible, reasoning = counter(usage.get('output')), counter(usage.get('reasoning'))
                    counts = {'input_tokens': counter(usage.get('input')), 'cached_input_tokens': counter(cache.get('read')),
                              'cache_write_input_tokens': counter(cache.get('write')),
                              'output_tokens': visible + reasoning if visible is not None and reasoning is not None else None,
                              'reasoning_output_tokens': reasoning}
                    value = part.get('cost')
                    cost = Decimal(str(value)) if type(value) in {int, float} and 0 <= value <= 10**9 and math.isfinite(value) else None
                    unit = {'tokens': counts, 'reported_cost': cost}
                    if key in self.units and unit != self.units[key]:
                        self.incomplete = True
                    self.units[key] = unit
                    self.open_steps.discard((session, message))
                    self._rebuild_opencode()
                    self.stream_complete = part.get('reason') == 'stop'
                    self.flush()

    def finish(self, status, exit_code=None):
        with self.lock:
            self.execution_status = status
            self.exit_code = exit_code
            self.finished_at = time.time()
            self.flush()


class JsonLines:
    """Drain arbitrarily large raw output while bounding each parsed event."""
    def __init__(self, collector):
        self.collector = collector
        self.buffer = bytearray()
        self.discarding = False

    def feed(self, chunk):
        for piece in chunk.splitlines(keepends=True):
            self.buffer.extend(piece) if not self.discarding else None
            if len(self.buffer) > MAX_EVENT:
                self.buffer.clear()
                self.discarding = True
                self.collector.oversized_events += 1
            if piece.endswith(b'\n'):
                if not self.discarding and self.buffer.strip():
                    try:
                        self.collector.consume(json.loads(self.buffer))
                    except (ValueError, UnicodeError, TypeError, KeyError, OverflowError, RecursionError):
                        self.collector.invalid_events += 1
                self.buffer.clear()
                self.discarding = False

    def finish(self):
        if self.buffer.strip() and not self.discarding:
            try:
                self.collector.consume(json.loads(self.buffer))
            except (ValueError, UnicodeError, TypeError, KeyError, OverflowError, RecursionError):
                self.collector.invalid_events += 1


def run_agent(argv, *, cwd, env, prompt, timeout, collector):
    """Drain both pipes concurrently; only a reduced usage.json reaches disk."""
    stop = threading.Event()
    failures = []
    parser = JsonLines(collector)
    def drain(stream, consume):
        try:
            while True:
                ready, _, _ = select.select([stream], [], [], .1)
                if not ready:
                    if stop.is_set():
                        break
                    continue
                data = os.read(stream.fileno(), 8192)
                if not data:
                    break
                if consume:
                    try:
                        parser.feed(data)
                    except ExecutionFailure:
                        collector.persistence_failed = True
            if consume:
                parser.finish()
        except ExecutionFailure:
            collector.persistence_failed = True
        except (OSError, ValueError, TypeError):
            failures.append('stream_unavailable')
    try:
        child = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
    except OSError:
        collector.finish('not_started')
        raise
    def write_prompt():
        try:
            pending = memoryview(prompt.encode())
            fd = child.stdin.fileno()
            os.set_blocking(fd, False)
            while pending and not stop.is_set():
                _, ready, _ = select.select([], [fd], [], .1)
                if ready:
                    try:
                        pending = pending[os.write(fd, pending):]
                    except BlockingIOError:
                        continue
        except (BrokenPipeError, OSError, ValueError):
            pass
        finally:
            child.stdin.close()
    threads = [threading.Thread(target=drain, args=(child.stdout, True), daemon=True),
               threading.Thread(target=drain, args=(child.stderr, False), daemon=True),
               threading.Thread(target=write_prompt, daemon=True)]
    for thread in threads:
        thread.start()
    timed_out = False
    try:
        try:
            child.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            child.kill()
            child.wait(timeout=5)
    finally:
        stop.set()
        for thread in threads:
            thread.join(timeout=2)
        if failures or any(thread.is_alive() for thread in threads):
            collector.incomplete = True
        for stream in (child.stdout, child.stderr):
            stream.close()
        collector.finish('timeout' if timed_out else 'completed' if child.returncode == 0 else 'failed', child.returncode)
    if collector.persistence_failed:
        raise ExecutionFailure('environment_telemetry_unavailable', phase='agent')
    if timed_out:
        raise subprocess.TimeoutExpired(argv, timeout)
    text = collector.response.decode(errors='replace') if not collector.response_overflow else ''
    return subprocess.CompletedProcess(argv, child.returncode, stdout=text)


def opencode_session_model(collector, executable, cwd, env):
    """Query only current owned session metadata, never messages or history."""
    if not collector.session_id:
        return
    argv = [executable, 'api', 'session.get', '--param', 'sessionID=' + collector.session_id]
    data = bytearray()
    child = None
    try:
        child = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            ready, _, _ = select.select([child.stdout], [], [], .1)
            if not ready:
                continue
            chunk = os.read(child.stdout.fileno(), 8192)
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > MAX_RECORD:
                return
        else:
            return
        if child.wait(timeout=.5):
            return
        result = json.loads(data).get('data')
        if not isinstance(result, dict) or result.get('id') != collector.session_id:
            return
        location = result.get('location', {}).get('directory')
        if not isinstance(location, str) or Path(location).resolve() != Path(cwd).resolve():
            return
        model = result.get('model') or {}
        provider, name = identifier(model.get('providerID')), identifier(model.get('id'))
        if provider and name:
            collector.reported_session_model = {'provider': provider, 'model': name,
                                                'source': 'opencode_session.get', 'scope': 'session_selection_not_per_step'}
            collector.flush()
    except (OSError, ValueError, TypeError, AttributeError, subprocess.TimeoutExpired):
        pass  # Metadata coverage remains explicitly unavailable.
    finally:
        if child is not None:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=2)
            child.stdout.close()
