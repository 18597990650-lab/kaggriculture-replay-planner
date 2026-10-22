"""Calibrate sale-event probabilities from completed public history and current state."""
import gzip
import hashlib
import json
import math

from compact_goods import feature_rows as public_rows, observation_key


def load_model(directory, record):
    raw = (directory / record['file']).read_bytes()
    if hashlib.sha256(raw).hexdigest() != record['sha256']:
        raise ValueError('Goods event model hash differs')
    model = json.loads(gzip.decompress(raw))
    if (model['schema'] != 'public-goods-logistic-v1' or model['width'] != 39 or
            model['horizons'] != [4, 8, 24] or model['thresholds'] != [1, 6]):
        raise ValueError('Unsupported goods event model contract')
    if len(model['fitted']) != 3 or any(len(row) != 7 for row in model['fitted']):
        raise ValueError('Invalid goods event model dimensions')
    for row in model['fitted']:
        for head in row:
            if len(head['linear']) != 39 or not all(math.isfinite(v) for v in head['linear']) or not math.isfinite(head['offset']):
                raise ValueError('Invalid goods event coefficients')
    return model


def feature_tensor(quantity, stock, current, history, observation, day, rows):
    public = public_rows(quantity, stock, current, history, observation, day)
    weights = quantity['cadence_weights']
    if len(rows) != 8 or len(weights) != 7 or any(len(row) != 8 for row in weights):
        raise ValueError('Goods event calibration requires the existing eight references')
    cumulatives = []
    for row in rows:
        running = [0] * 7
        totals = []
        for slot in range(6):
            for pi in range(7):
                running[pi] += row[9 + slot * 9 + pi + 1]
            if slot in (0, 1, 5):
                totals.append(tuple(running))
        cumulatives.append(totals)
    tensor = []
    for hi in range(3):
        thresholds = []
        for threshold in (1, 6):
            products = []
            for pi in range(7):
                probability = sum(w for w, values in zip(weights[pi], cumulatives) if values[hi][pi] >= threshold)
                safe = max(.001, min(.999, probability))
                products.append((1., math.log(safe / (1 - safe)), threshold / 6,
                                 *public[pi][3:], *(float(public[pi][27 + lag * 3 + hi] < 0) for lag in range(3))))
            thresholds.append(tuple(products))
        tensor.append(tuple(thresholds))
    return tuple(tensor)


def coherent(values):
    result = [[[float(v) for v in row] for row in horizon] for horizon in values]
    for ti in range(2):
        for pi in range(7):
            original = [result[h][ti][pi] for h in range(3)]
            left, middle, right = original
            first = left > middle
            if first:
                left = middle = (left + middle) / 2
            last = middle > right
            if last:
                middle = right = (middle + right) / 2
            if (first and last) or left > middle:
                left = middle = right = sum(original) / 3
            for hi, value in enumerate((left, middle, right)):
                result[hi][ti][pi] = value
    for hi in range(3):
        for pi in range(7):
            if result[hi][0][pi] < result[hi][1][pi]:
                average = (result[hi][0][pi] + result[hi][1][pi]) / 2
                result[hi][0][pi] = result[hi][1][pi] = average
    return tuple(tuple(tuple(row) for row in horizon) for horizon in result)


def predict(model, features):
    result = []
    for hi in range(3):
        thresholds = []
        for ti in range(2):
            values = []
            for pi, head in enumerate(model['fitted'][hi]):
                score = sum(a * b for a, b in zip(features[hi][ti][pi], head['linear'])) + head['offset']
                values.append(1 / (1 + math.exp(-max(-40., min(40., score)))))
            thresholds.append(tuple(values))
        result.append(tuple(thresholds))
    return coherent(result)


def apply(model, forecast, quantity, stock, current, history, observation, day, rows):
    if forecast is None:
        return
    if (quantity is None or quantity.get('compact_goods_status') != 'applied' or
            quantity.get('cadence_status') != 'applied' or observation_key(current, history, observation, day) is None):
        forecast['goods_event_status'] = 'unavailable_public_base_or_history'
        return
    if forecast['horizons'] != [4, 8, 24] or forecast['target'] != 'successful_sell_minus_successful_buy':
        raise ValueError('Goods event calibration requires cumulative dawn net-sale targets')
    features = feature_tensor(quantity, stock, current, history, observation, day, rows)
    updated = predict(model, features)
    original = forecast['probabilities']
    output = [[list(map(list, direction)) for direction in horizon] for horizon in original]
    for hi in range(3):
        for ti in range(2):
            output[hi][0][ti][1:8] = updated[hi][ti]
    forecast['probabilities'] = tuple(tuple(tuple(tuple(row) for row in direction) for direction in horizon) for horizon in output)
    forecast['goods_event_original'] = tuple(tuple(tuple(row[1:8]) for row in horizon[0]) for horizon in original)
    forecast['goods_event_status'] = 'applied'
    forecast['goods_event_boundary'] = ('Frozen product/horizon logits use existing K8 cadence weights and current public inputs. '
        'Includes one-dollar successful sales. Learned quantity outputs, query identity and private rival state are excluded. '
        'Only seven non-purchasable goods sell probabilities change; cumulative horizons and thresholds are nested.')
