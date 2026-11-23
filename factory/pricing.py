"""Offline API-equivalent estimates from explicit, frozen pricing catalogs.

Canonical input counters are disjoint (fresh, cache read, cache write). Output
already includes reasoning; reasoning_output_tokens is informational only.
No requested model or live price lookup is used as an implicit fallback.
"""

import copy
from datetime import date
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import json
import re
from urllib.parse import urlsplit


KIND = 'api_equivalent_estimate_not_invoice'
MAX_CATALOG_BYTES = 16 * 1024
COMPONENTS = {
    'input_tokens': 'input_per_unit',
    'cached_input_tokens': 'cached_input_per_unit',
    'cache_write_input_tokens': 'cache_write_per_unit',
    'output_tokens': 'output_per_unit',
}


def _identifier(value, name):
    if (not isinstance(value, str)
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:+/-]{0,199}', value)
            or re.match(r'^[A-Za-z]:/', value)
            or any(part in {'.', '..'} or part.lower().startswith(('sk-', 'ghp_', 'github_pat_'))
                   for part in value.split('/'))):
        raise ValueError(f'{name} must be a bounded public identifier')
    return value


def _decimal(value, name, positive=False):
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise ValueError(f'{name} must be an exact decimal string or integer')
    if len(str(value)) > 200:
        raise ValueError(f'{name} is too long')
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f'{name} must be decimal') from exc
    if not result.is_finite() or result < 0 or (positive and result == 0):
        raise ValueError(f'{name} must be finite and {"positive" if positive else "nonnegative"}')
    # Bound exponents to keep arithmetic and JSON serialization predictable.
    if abs(result.as_tuple().exponent) > 100 or result.adjusted() > 100:
        raise ValueError(f'{name} exponent is out of bounds')
    return result


def _text(value):
    return format(value, 'f')


def freeze_catalog(config):
    """Validate and detach usage_pricing, adding a reproducible content hash.

    Missing rates are preserved as null, never interpreted as free. All monetary
    values are strings so the returned snapshot round-trips through JSON exactly.
    """
    raw = config.get('usage_pricing')
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError('usage_pricing must be an object')
    version = _identifier(raw.get('version'), 'catalog version')
    if not isinstance(raw.get('rates'), list) or len(raw['rates']) > 10000:
        raise ValueError('catalog rates must be a list of at most 10000 entries')
    rates, seen = [], set()
    for raw_rate in raw['rates']:
        if not isinstance(raw_rate, dict):
            raise ValueError('each catalog rate must be an object')
        entry = {key: _identifier(raw_rate.get(key), key) for key in ('provider', 'model')}
        identity = (entry['provider'], entry['model'])
        if identity in seen:
            raise ValueError('duplicate provider/model in catalog')
        seen.add(identity)
        currency = raw_rate.get('currency')
        if not isinstance(currency, str) or not re.fullmatch(r'[A-Z]{3}', currency):
            raise ValueError('currency must be a three-letter uppercase code')
        entry['currency'] = currency
        entry['unit_tokens'] = _text(_decimal(raw_rate.get('unit_tokens'), 'unit_tokens', positive=True))
        # A terminating exact decimal is required for a decimal-valued estimate.
        denominator = Decimal(entry['unit_tokens'])
        numerator, _ = denominator.as_integer_ratio()
        for factor in (2, 5):
            while numerator % factor == 0:
                numerator //= factor
        if numerator != 1:
            raise ValueError('unit_tokens must permit exact decimal division (powers of 2 and 5)')
        for field in COMPONENTS.values():
            value = raw_rate.get(field)
            entry[field] = None if value is None else _text(_decimal(value, field))
        source = raw_rate.get('source_url')
        if (not isinstance(source, str) or len(source) > 2048
                or any(c.isspace() or ord(c) < 32 for c in source)):
            raise ValueError('source_url must identify the HTTPS primary pricing source')
        try:
            parsed = urlsplit(source)
            valid_source = (parsed.scheme == 'https' and bool(parsed.hostname)
                            and parsed.username is None and parsed.password is None
                            and '?' not in source.split('#', 1)[0])
        except ValueError:
            valid_source = False
        if not valid_source:
            raise ValueError('source_url must identify the HTTPS primary pricing source')
        entry['source_url'] = source
        effective = raw_rate.get('effective_date')
        try:
            if not isinstance(effective, str) or date.fromisoformat(effective).isoformat() != effective:
                raise ValueError
        except ValueError as exc:
            raise ValueError('effective_date must be YYYY-MM-DD') from exc
        entry['effective_date'] = effective
        rates.append(entry)
    snapshot = {'version': version, 'rates': sorted(rates, key=lambda item: (item['provider'], item['model']))}
    encoded = json.dumps(snapshot, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()
    snapshot['catalog_sha256'] = hashlib.sha256(encoded).hexdigest()
    if len(json.dumps(snapshot, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()) > MAX_CATALOG_BYTES:
        raise ValueError('pricing catalog snapshot exceeds 16 KiB')
    return snapshot


def _counter(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def estimate_cost(groups, catalog, assumption=None):
    """Estimate observed groups, preserving missing evidence and currency units."""
    if assumption is not None:
        if not isinstance(assumption, dict) or set(assumption) != {'provider', 'model'}:
            raise ValueError('assumption must contain provider and model')
        assumption = {key: _identifier(assumption.get(key), key) for key in ('provider', 'model')}
    # Revalidate and detach even a stored snapshot. Its hash must match its rates.
    frozen = freeze_catalog({'usage_pricing': catalog}) if catalog is not None else None
    if catalog is not None and catalog.get('catalog_sha256') not in (None, frozen['catalog_sha256']):
        raise ValueError('catalog snapshot hash does not match its contents')
    lookup = {(rate['provider'], rate['model']): rate for rate in frozen['rates']} if frozen else {}
    lines, totals, currency_known = [], {}, {}
    complete, known = True, False
    for group in groups:
        provider, model = group.get('provider'), group.get('model')
        rate = lookup.get((provider, model))
        basis = 'observed_model' if rate else None
        if rate is None and assumption is not None and (provider is None or model is None):
            rate = lookup.get((assumption['provider'], assumption['model']))
            basis = 'configured_assumption'
        tokens = group.get('tokens') or {}
        components, subtotal = [], Decimal(0)
        line_complete, line_known = True, False
        for counter, price_field in COMPONENTS.items():
            count = _counter(tokens.get(counter))
            price = rate[price_field] if rate else None
            component = {'counter': counter, 'tokens': count, 'rate_per_unit': price}
            if count == 0:
                component.update(status='estimated', amount='0')
                line_known = True
            elif count is None or price is None:
                component.update(status='unpriced', reason='unknown_tokens' if count is None else 'missing_rate')
                line_complete = False
            else:
                # Validation bounds rate lengths/exponents; token size contributes
                # to precision so even very large integer counts remain exact.
                with localcontext() as ctx:
                    ctx.prec = max(1000, len(str(count)) + 1000)
                    amount = Decimal(count) * Decimal(price) / Decimal(rate['unit_tokens'])
                    subtotal += amount
                component.update(status='estimated', amount=_text(amount))
                line_known = True
            components.append(component)
        status = 'estimated' if line_complete else ('partial' if line_known else 'unpriced')
        line = {'provider': provider, 'model': model, 'basis': basis,
                'status': status, 'components': components,
                'reasoning_output_tokens': _counter(tokens.get('reasoning_output_tokens')),
                'catalog_version': frozen['version'] if frozen else None,
                'catalog_sha256': frozen['catalog_sha256'] if frozen else None,
                'rate': copy.deepcopy(rate), 'currency': rate['currency'] if rate else None}
        if rate is not None:
            if line_known:
                line['amount' if line_complete else 'known_amount'] = _text(subtotal)
            with localcontext() as ctx:
                ctx.prec = max(1000, len(str(subtotal)) + len(str(totals.get(rate['currency'], 0))) + 10)
                totals[rate['currency']] = totals.get(rate['currency'], Decimal(0)) + subtotal
            currency_known[rate['currency']] = currency_known.get(rate['currency'], False) or line_known
            known = known or line_known
        elif line_complete:
            # All-zero evidence has zero cost without needing a rate/currency.
            line['amount'] = '0'
            known = True
        else:
            line['reason'] = 'missing_catalog' if frozen is None else 'unknown_model'
        complete = complete and line_complete
        lines.append(line)
    status = 'estimated' if complete and lines else ('partial' if known else 'unpriced')
    currencies = []
    for currency, amount in sorted(totals.items()):
        row = {'currency': currency, 'status': 'estimated' if complete else
               ('partial' if currency_known[currency] else 'unpriced')}
        if currency_known[currency]:
            row['amount' if complete else 'known_amount'] = _text(amount)
        currencies.append(row)
    return {'kind': KIND, 'status': status, 'currencies': currencies, 'lines': lines,
            'catalog_version': frozen['version'] if frozen else None,
            'catalog_sha256': frozen['catalog_sha256'] if frozen else None}
