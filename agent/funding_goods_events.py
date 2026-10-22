"""Sale probability calibration with public cross-product and resource context."""
import gzip
import hashlib
import json
import math

from compact_goods import feature_rows as public_rows, observation_key as public_key
from goods_events import feature_tensor as base_tensor, predict, apply as fallback_apply
from funding_context import features as context_features, TIMING_NAMES, FUNDING_NAMES


def load_model(directory, record):
    raw = (directory / record['file']).read_bytes()
    if hashlib.sha256(raw).hexdigest() != record['sha256']:
        raise ValueError('Funding event model hash differs')
    model = json.loads(gzip.decompress(raw))
    width = 39 + len(TIMING_NAMES) + len(FUNDING_NAMES)
    if (model['schema'] != 'public-funding-goods-events-v1' or model['width'] != width or
            model['horizons'] != [4, 8, 24] or model['thresholds'] != [1, 6] or
            model['context_names'] != list(TIMING_NAMES + FUNDING_NAMES)):
        raise ValueError('Unsupported funding event contract')
    if len(model['fitted']) != 3 or any(len(row) != 7 for row in model['fitted']):
        raise ValueError('Invalid funding event head count')
    for row in model['fitted']:
        for head in row:
            if len(head['linear']) != width or not all(math.isfinite(v) for v in head['linear']) or not math.isfinite(head['offset']):
                raise ValueError('Invalid funding event coefficients')
    return model


def observation_key(current, history, observation, day):
    if int(observation['step']) % 24 or public_key(current, history, observation, day) is None:
        return None
    cargo = observation.get('_public_rival_cargo', {})
    grain, flow = cargo.get('wheat_balance'), cargo.get('resource_flow')
    if not grain or not flow or grain.get('origin') != day * 24 or flow.get('origin') != day * 24:
        return None
    if not grain.get('started_at_zero') or not flow.get('started_at_zero') or flow.get('window_steps') != 24:
        return None
    return (grain['point'], grain['unknown_trades'], tuple(flow['known_buy_last24']),
        tuple(flow['known_sell_last24']), tuple(flow['observed_steps_last24']),
        tuple(history[day - 1]['assets']), tuple(history[day - 3]['assets']), history[day - 1]['cash'])


def feature_tensor(quantity, stock, current, history, observation, day, rows):
    base = base_tensor(quantity, stock, current, history, observation, day, rows)
    public = public_rows(quantity, stock, current, history, observation, day)
    cargo = observation['_public_rival_cargo']
    extra = context_features(public, current, history[day - 1], history[day - 3],
        cargo['wheat_balance'], cargo['resource_flow'], day)
    return tuple(tuple(tuple((*row, *extra[pi]) for pi, row in enumerate(threshold))
        for threshold in horizon) for horizon in base)


def apply(model, fallback_model, forecast, quantity, stock, current, history, observation, day, rows):
    if forecast is None:
        return
    if (quantity is None or quantity.get('compact_goods_status') != 'applied' or
            quantity.get('cadence_status') != 'applied' or
            observation_key(current, history, observation, day) is None):
        fallback_apply(fallback_model, forecast, quantity, stock, current, history, observation, day, rows)
        forecast['funding_event_status'] = 'unavailable_public_context'
        return
    if forecast['horizons'] != [4, 8, 24] or forecast['target'] != 'successful_sell_minus_successful_buy':
        raise ValueError('Funding calibration requires cumulative dawn sale targets')
    updated = predict(model, feature_tensor(quantity, stock, current, history, observation, day, rows))
    original = forecast['probabilities']
    output = [[list(map(list, direction)) for direction in horizon] for horizon in original]
    for hi in range(3):
        for ti in range(2):
            output[hi][0][ti][1:8] = updated[hi][ti]
    forecast['probabilities'] = tuple(tuple(tuple(tuple(row) for row in direction) for direction in horizon) for horizon in output)
    forecast['goods_event_original'] = tuple(tuple(tuple(row[1:8]) for row in horizon[0]) for horizon in original)
    forecast['goods_event_status'] = 'applied'
    forecast['goods_event_boundary'] = 'Frozen public cross-product timing and resource context; no opponent-private inputs, learned quantity points or future actions.'
    forecast['funding_event_status'] = 'applied'
