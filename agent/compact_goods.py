"""Low-cost public-state correction of seven cumulative sale forecasts."""
import gzip
import hashlib
import json
import math
from cadence_prior import observation_key as cadence_key

BASE = (35, 60, 120, 250, 50, 160, 200)
CONSUMPTION = ((0, 0, 0, 0, 0, 2, 0, 1), (0, 1, 0, 0, 0, 0, 0, 1),
               (0, 0, 1, 0, 1, 0, 1, 1), (0, 0, 0, 0, 0, 0, 0, 0),
               (1, 0, 1, 0, 0, 0, 0, 0), (0, 1, 0, 0, 1, 0, 1, 0), (0, 0, 0, 2, 0, 0, 0, 0))


def load_model(directory, record):
    raw = (directory / record['file']).read_bytes()
    if hashlib.sha256(raw).hexdigest() != record['sha256']:
        raise ValueError('Compact quantity model hash differs')
    model = json.loads(gzip.decompress(raw))
    if (model['schema'] != 'public-small-residual-trees-v1' or model['depth'] != 2 or model['selected'] != '16'
            or model['learning_rate'] != .2 or model['unit_scale'] != 6 or len(model['features']) != 36):
        raise ValueError('Unsupported compact quantity contract')

    def verify(node, depth):
        if 'value' in node:
            if len(node['value']) != 3 or not all(math.isfinite(v) and abs(v) <= 2 for v in node['value']):
                raise ValueError('Invalid compact quantity leaf')
            return
        if depth >= 2 or not 0 <= node['feature'] < 36 or not math.isfinite(node['threshold']):
            raise ValueError('Invalid compact quantity branch')
        verify(node['left'], depth + 1); verify(node['right'], depth + 1)

    if len(model['trees']) != 7 or any(len(ensemble) != 16 for ensemble in model['trees']):
        raise ValueError('Invalid compact quantity ensemble dimensions')
    for ensemble in model['trees']:
        for tree in ensemble:
            verify(tree, 0)
    return model


def observation_key(current, history, observation, day):
    if cadence_key(observation) is None or any(d not in history for d in (day - 1, day - 3)):
        return None
    keys = ('assets', 'ready', 'stress', 'competitor', 'shops', 'quadrants', 'maturity', 'phase', 'prices')
    return (current['cash'], *(tuple(current[key]) for key in keys),
            tuple(history[day - 1]['prices']), tuple(history[day - 3]['prices']))


def feature_rows(quantity, stock, current, history, observation, day):
    result = []; previous_sales = dict(observation['_public_rival_cargo']['goods_flow']['recent_completed'])
    log_cash = math.log1p(max(0, current['cash']) / 1000)
    crop_workload = sum(current['assets'][:5]) / 20; herd_workload = sum(current['assets'][5:8]) / 10
    for pi in range(7):
        p = pi + 1; assets = current['assets'][p]; price = current['prices'][p]
        stored = stock['point_estimate'][pi]; cap = quantity['estimated_mature_goods_capacity'][pi]
        values = [*(row[p] / 6 for row in quantity['point_estimate']), stored / 6, (cap - stored) / 6, cap / 6,
            price / BASE[pi], (price - history[day - 1]['prices'][p]) / BASE[pi],
            (price - history[day - 3]['prices'][p]) / BASE[pi], log_cash, assets / 4, current['ready'][p] / 6,
            current['stress'][p] / 4, current['competitor'][p] / 4, crop_workload, herd_workload,
            sum(a * b for a, b in zip(current['shops'], CONSUMPTION[pi])) / 4, day / 30,
            max(current['quadrants'][q * 9 + p] for q in range(4)) / max(1, assets)]
        values.extend(current['maturity'][q * 9 + p] / 4 for q in range(4))
        values.extend(current['phase'][q * 9 + p] / 4 for q in range(4))
        for lag in (1, 2, 3):
            windows = previous_sales[day - lag]
            values.extend(windows[h][pi] / 6 if windows[h][pi] is not None else -1. for h in range(3))
        if len(values) != 36 or not all(math.isfinite(value) for value in values):
            raise ValueError('Invalid compact public quantity features')
        result.append(tuple(values))
    return tuple(result)


def apply(model, quantity, stock, current, history, observation, day):
    if quantity is None:
        return
    if (quantity.get('cadence_status') != 'applied' or quantity.get('adaptive_goods_status') != 'applied'
            or observation_key(current, history, observation, day) is None):
        quantity['compact_goods_status'] = 'unavailable_public_base_or_history'
        return
    features = feature_rows(quantity, stock, current, history, observation, day)
    original = quantity['point_estimate']; output = [list(row) for row in original]
    for pi, (values, ensemble) in enumerate(zip(features, model['trees'])):
        cap = quantity['estimated_mature_goods_capacity'][pi] / 6
        current_values = [row[pi + 1] / 6 for row in original]
        for tree in ensemble:
            node = tree
            while 'value' not in node:
                node = node['left'] if values[node['feature']] <= node['threshold'] else node['right']
            for hi in range(3):
                value = max(0., min(cap, current_values[hi] + .2 * node['value'][hi]))
                current_values[hi] = max(value, current_values[hi - 1]) if hi else value
        for hi in range(3):
            output[hi][pi + 1] = current_values[hi] * 6
    quantity['point_estimate'] = tuple(tuple(row) for row in output)
    quantity['compact_goods_status'] = 'applied'
    quantity['compact_goods_original'] = tuple(tuple(row[1:8]) for row in original)
    quantity['compact_goods_boundary'] = 'Frozen depth2 residual trees use current public prices,cash,assets andcompleted public sale history. No additional neighbor search orcurrent-rival private state. Original adaptive-learning experts are unchanged.'
