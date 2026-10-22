"""Calibrate wheat purchases from public grain while preserving sale probabilities."""
import gzip
import hashlib
import json
import math
from relative_grain import observation_key
from pressure_forecast import logit


def load_model(directory, record):
    raw = (directory / record['file']).read_bytes()
    if hashlib.sha256(raw).hexdigest() != record['sha256']:
        raise ValueError('Grain purchase model hash mismatch')
    model = json.loads(gzip.decompress(raw))
    if model['schema'] != 'public-grain-net-buy-v1' or len(model['linear']) != 3 or len(model['offset']) != 3:
        raise ValueError('Grain purchase model contract mismatch')
    if any(len(row) != 18 or any(not math.isfinite(value) for value in row) for row in model['linear']):
        raise ValueError('Invalid grain purchase coefficients')
    if not all(math.isfinite(value) for value in model['offset']):
        raise ValueError('Invalid grain purchase offsets')
    return model


def apply(model, head, quantity, pressure, rows, weights, distances, public, observation, day):
    if head is None or int(observation['step']) % 24:
        return
    key = observation_key(observation)
    if key is None or quantity is None or quantity.get('relative_grain_status') != 'applied':
        head['wheat_buy_status'] = 'unavailable_public_grain'
        return
    if head['target'] != 'successful_sell_minus_successful_buy' or head['horizons'] != [4, 8, 24]:
        raise ValueError('Grain purchase calibration requires dawn net-trade horizons')
    if len(rows) != 8 or len(weights) != 8 or len(distances) != 8:
        raise ValueError('Grain purchase calibration requires the existing eight neighbors')
    grain, unknown, ambiguous, net24, count24, net8, count8 = key
    reference = quantity['public_grain_estimate']
    animals = sum(public['assets'][5:8])
    extra = (-net24 / 12, count24 / 24, -net8 / 6, count8 / 8,
             -(math.log1p(grain / 6) - math.log1p(reference['reference_median'] / 6)),
             -(max(0., animals - grain) - reference['reference_feeding_shortfall']) / 6,
             math.log1p(unknown), math.log1p(ambiguous))
    minimum = math.log1p(min(distances))
    average = math.log1p(sum(distance * weight for distance, weight in zip(distances, weights)))
    old_buy = []
    for hi, slots in enumerate((1, 2, 6)):
        quantities = [row[0] if slots == 6 else sum(row[9 + slot * 9] for slot in range(slots)) for row in rows]
        old_buy.append(tuple(head['probabilities'][hi][1][ti][0] for ti in range(2)))
        values = []
        for ti, threshold in enumerate((1, 6)):
            probability = sum(weight for amount, weight in zip(quantities, weights) if amount <= -threshold)
            score = logit(probability)
            prior = logit(pressure['priors'][day - 6][hi][1][ti][0])
            features = (1., score, prior, minimum, average, (score - prior) * average,
                        0., 0., math.log1p(public['prices'][0] / 25), -1.) + extra
            value = model['offset'][hi] + sum(a * b for a, b in zip(model['linear'][hi], features))
            values.append(1. / (1. + math.exp(-min(40., max(-40., value)))))
        values[0] = min(values[0], 1. - head['probabilities'][hi][0][0][0])
        values[1] = min(values[1], values[0])
        for ti in range(2):
            head['probabilities'][hi][1][ti][0] = values[ti]
    head['wheat_buy_status'] = 'applied'
    head['wheat_buy55_reference'] = tuple(old_buy)
    head['wheat_buy_boundary'] = 'Only WHEAT net-buy probabilities change. Original net-sell probabilities are preserved and impose the complementary-probability cap. Publicly inferred grain is not an inventory guarantee.'
