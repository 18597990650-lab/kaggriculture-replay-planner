"""Condition wheat net trades on grain held relative to matched public routes."""
import gzip
import hashlib
import json
import math
import struct


def load_model(directory, record, training_samples, training_order):
    packed = (directory / record['file']).read_bytes()
    if hashlib.sha256(packed).hexdigest() != record['sha256']:
        raise ValueError('Relative grain model hash mismatch')
    model = json.loads(gzip.decompress(packed))
    if model['schema'] != 'public-relative-grain-v1' or model['training_order_sha256'] != training_order:
        raise ValueError('Relative grain model contract mismatch')
    coefficients = model['coefficient']
    if len(coefficients) != 3 or any(len(row) != 20 or any(not math.isfinite(value) for value in row) for row in coefficients):
        raise ValueError('Invalid relative grain coefficients')
    bank = record['bank']
    raw = (directory / bank['file']).read_bytes()
    if hashlib.sha256(raw).hexdigest() != bank['sha256']:
        raise ValueError('Relative grain reference bank hash mismatch')
    payload = gzip.decompress(raw)
    magic, count, days, width = struct.unpack_from('<4s3I', payload)
    if (magic, count, days, width) != (b'GR01', training_samples, 30, 2):
        raise ValueError('Relative grain reference bank shape mismatch')
    if len(payload) != 16 + 2 * count * days * width:
        raise ValueError('Truncated relative grain reference bank')
    model['bank_values'] = struct.unpack_from(f'<{count * days * width}H', payload, 16)
    return model


def observation_key(observation):
    step = int(observation['step'])
    if step % 24:
        return None
    cargo = observation.get('_public_rival_cargo', {})
    flow, grain = cargo.get('resource_flow'), cargo.get('wheat_balance')
    if not flow or not grain or flow.get('origin') != step or grain.get('origin') != step:
        return None
    if not flow.get('started_at_zero') or not grain.get('started_at_zero') or flow.get('window_steps') != 24:
        return None
    return (grain['point'], grain['unknown_trades'], grain['ambiguous_harvests'],
            flow['known_net_last24'][0], flow['observed_steps_last24'][0],
            flow['known_net_last8'][0], flow['observed_steps_last8'][0])


def weighted_median(values, weights):
    cumulative = 0.
    pairs = sorted(zip(values, weights))
    for value, weight in pairs:
        cumulative += weight
        if cumulative >= .5:
            return value
    return pairs[-1][0]


def apply(model, quantity, rows, neighbors, weights, public, observation, day):
    if quantity is None:
        return
    key = observation_key(observation)
    if key is None:
        quantity['relative_grain_status'] = 'unavailable_public_history'
        return
    if len(rows) != 8 or len(neighbors) != 8 or len(weights) != 8:
        raise ValueError('Relative grain calibration requires the existing eight neighbors')
    grain, unknown, ambiguous, net24, count24, net8, count8 = key
    bank = model['bank_values']
    reference_stock = [bank[(index * 30 + day) * 2] for index in neighbors]
    reference_shortfall = sum(bank[(index * 30 + day) * 2 + 1] * weight for index, weight in zip(neighbors, weights))
    median_stock = weighted_median(reference_stock, weights)
    animals = sum(public['assets'][5:8])
    extra = (net24 / 12, count24 / 24, net8 / 6, count8 / 8,
             math.log1p(grain / 6) - math.log1p(median_stock / 6),
             (max(0., animals - grain) - reference_shortfall) / 6,
             math.log1p(unknown), math.log1p(ambiguous))
    before = quantity['point_estimate']
    output = [list(row) for row in before]
    for hi, slots in enumerate((1, 2, 6)):
        amounts = [sum(row[9 + slot * 9] for slot in range(slots)) for row in rows]
        median = weighted_median(amounts, weights)
        sell = sum(weight for amount, weight in zip(amounts, weights) if amount >= 1)
        buy = sum(weight for amount, weight in zip(amounts, weights) if amount <= -1)
        mean = quantity['raw_neighbor_mean'][hi][0]
        features = (1., mean / 6, median / 6, sell, buy, math.log1p(public['prices'][0] / 25),
                    math.log1p(max(0., public['cash']) / 1000), public['ready'][0] / 6,
                    animals / 4, sum(public['fertilizer'][5:8]) / 6, public['assets'][0] / 4, day / 30) + extra
        output[hi][0] = mean + 6 * sum(value * coefficient for value, coefficient in zip(features, model['coefficient'][hi]))
    quantity['point_estimate'] = tuple(tuple(row) for row in output)
    quantity['wheat55_reference'] = tuple(row[0] for row in before)
    quantity['relative_grain_status'] = 'applied'
    quantity['public_grain_estimate'] = dict(point=grain, unknown_trades=unknown, ambiguous_harvests=ambiguous,
                                            reference_median=median_stock, reference_feeding_shortfall=reference_shortfall)
    quantity['relative_grain_boundary'] = 'Only WHEAT net SELL minus BUY is adjusted. Current stock is inferred from public changes; historical stock is a training label. Estimates are neither inventory bounds nor executable orders.'
