"""Condition existing neighbors on completed public sales timing."""
import gzip
import hashlib
import json
import math
import struct
import sys

PRODUCTS = ('CARROT', 'TOMATO', 'STRAWBERRY', 'MELON', 'EGG', 'MILK', 'WOOL')


def load_model(directory, record, training_samples, training_order):
    raw = (directory / record['file']).read_bytes()
    if hashlib.sha256(raw).hexdigest() != record['sha256']:
        raise ValueError('Cadence parameter hash mismatch')
    model = json.loads(raw)
    if (model['schema'] != 'public-goods-cadence-v1' or model['training_order_sha256'] != training_order
            or model['strength'] != 2. or model['history_days'] != 3 or model['scales'] != [6, 6, 24]):
        raise ValueError('Cadence model contract differs')
    bank = record['bank']; packed = (directory / bank['file']).read_bytes()
    if hashlib.sha256(packed).hexdigest() != bank['sha256']:
        raise ValueError('Cadence historical bank hash mismatch')
    payload = gzip.decompress(packed)
    if struct.unpack_from('<4s4I', payload) != (b'CT01', training_samples, 30, 3, 7):
        raise ValueError('Cadence historical dimensions differ')
    if len(payload) != 20 + 2 * training_samples * 30 * 3 * 7 or sys.byteorder != 'little':
        raise ValueError('Unsupported cadence bank representation')
    model['bank'] = memoryview(payload)[20:].cast('H')
    model['training_samples'] = training_samples
    return model


def observation_key(observation):
    tick = int(observation['step']); day = tick // 24
    if tick % 24:
        return None
    flow = observation.get('_public_rival_cargo', {}).get('goods_flow')
    if not flow or flow.get('origin') != tick or not flow.get('started_at_zero'):
        return None
    history = flow.get('recent_completed')
    if history is None or [entry[0] for entry in history] != list(range(day - 3, day)):
        return None
    if tuple(flow['products']) != PRODUCTS:
        raise ValueError('Cadence public products differ')
    records = []
    for when, windows in history:
        if len(windows) != 3 or any(len(row) != 7 for row in windows):
            raise ValueError('Cadence public windows require3 horizons and7 products')
        bins = []
        for pi in range(7):
            a, b, c = (windows[hi][pi] for hi in range(3))
            if any(v is not None and (not isinstance(v, int) or v < 0) for v in (a, b, c)):
                raise ValueError('Certified historical sales must be nonnegative integers')
            values = (a, b - a if a is not None and b is not None else None,
                      c - b if b is not None and c is not None else None)
            if any(v is not None and v < 0 for v in values):
                raise ValueError('Cumulative goods sales decreased')
            bins.append(values)
        records.append((when, tuple(bins)))
    return tuple(records)


def apply(model, calibration, quantity, rows, neighbors, weights, observation, day):
    if quantity is None:
        return
    history = observation_key(observation)
    if history is None or quantity.get('goods_reference_status') != 'applied':
        quantity['cadence_status'] = 'unavailable_public_history_or_reference'
        return
    if not len(rows) == len(neighbors) == len(weights) == 8:
        raise ValueError('Cadence reweighting requires theexisting eight neighbors')
    if any(index < 0 or index >= model['training_samples'] for index in neighbors):
        raise ValueError('Cadence reference index is outside frozen training order')
    original = quantity['point_estimate']; output = [list(row) for row in original]
    projected = [[0.] * 7 for _ in range(3)]
    memberships = quantity['route_probabilities']; coefficients = calibration['coefficient']
    bank = model['bank']; posterior = []; counts = []
    for pi in range(7):
        known = [(when, bi, value) for when, bins in history for bi, value in enumerate(bins[pi]) if value is not None]
        counts.append(len(known))
        logs = []
        for index, weight in zip(neighbors, weights):
            loss = sum(abs(bank[(index * 30 + when) * 21 + bi * 7 + pi] - value) / model['scales'][bi]
                       for when, bi, value in known) / max(1, len(known))
            logs.append(math.log(weight) - model['strength'] * loss)
        maximum = max(logs); adjusted = [math.exp(value - maximum) for value in logs]; total = sum(adjusted)
        adjusted = tuple(value / total for value in adjusted); posterior.append(adjusted)
        for hi, slots in enumerate((1, 2, 6)):
            values = sorted((sum(row[9 + slot * 9 + pi + 1] for slot in range(slots)), weight)
                            for row, weight in zip(rows, adjusted))
            mean = sum(value * weight for value, weight in values)
            accumulated = 0.; median = values[-1][0]
            for value, weight in values:
                accumulated += weight
                if accumulated >= .5:
                    median = value
                    break
            capacity = quantity['estimated_goods_capacity'][pi]
            raw = max(0., min(capacity, mean)); base = raw / 6; present = float(capacity > 0)
            features = (base, min(capacity, median) / 6, capacity / 6, present,
                        *(w * base for w in memberships), *(w * present for w in memberships))
            value = max(0., min(capacity, raw + 6 * sum(a * b for a, b in zip(features, coefficients[hi][pi]))))
            projected[hi][pi] = max(value, projected[hi - 1][pi]) if hi else value
        for hi in range(3):
            value = min(projected[hi][pi], quantity['estimated_mature_goods_capacity'][pi])
            projected[hi][pi] = value
            output[hi][pi + 1] = .75 * value + .25 * quantity['goods_reference_experts']['new'][hi][pi]
    quantity['point_estimate'] = tuple(tuple(row) for row in output)
    quantity['cadence_status'] = 'applied'
    quantity['cadence_weights'] = tuple(posterior)
    quantity['cadence_known_bins'] = tuple(counts)
    quantity['cadence_experts'] = dict(old=tuple(tuple(row[1:8]) for row in original), reweighted_base=tuple(tuple(row) for row in projected))
    quantity['cadence_boundary'] = 'Only completed prior3-day public goods sales rerank theexistingK8 neighbors perproduct. Stock,capacity,cohort weights andsupplementary component remain fixed. No additional retrieval orprivate query input.'
