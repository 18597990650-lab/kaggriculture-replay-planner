"""Temper stock-based sales corrections with certified completed-day feedback."""
import gzip
import hashlib
import json
import math

PRODUCTS = ('CARROT', 'TOMATO', 'STRAWBERRY', 'MELON', 'EGG', 'MILK', 'WOOL')


def load_model(directory, record):
    raw = (directory / record['file']).read_bytes()
    if hashlib.sha256(raw).hexdigest() != record['sha256']:
        raise ValueError('Adaptive goods model hash mismatch')
    model = json.loads(gzip.decompress(raw))
    if model['schema'] != 'public-adaptive-goods-stock-v1':
        raise ValueError('Unsupported adaptive goods schema')
    if model['history_days'] != 3 or model['absolute_loss_scale'] != 6 or model['prior'] != .75:
        raise ValueError('Adaptive goods feedback contract differs')
    coefficient = model['coefficient']
    if len(coefficient) != 3 or any(len(row) != 7 or any(not math.isfinite(v) or not 0 <= v <= 1 for v in row) for row in coefficient):
        raise ValueError('Invalid bounded relative-stock coefficients')
    return model


class AdaptiveGoodsStock:
    def __init__(self, model):
        self.model = model
        self.reset()

    def reset(self):
        self.day = None
        self.pending = None
        self.losses = []
        self.scored_cells = 0

    def observe(self, observation):
        tick = int(observation['step']); day = tick // 24
        if tick % 24 or self.day == day:
            return
        if self.day is not None and day < self.day:
            raise ValueError('Reset adaptive goods before starting another game')
        flow = observation.get('_public_rival_cargo', {}).get('goods_flow')
        valid = bool(flow and flow.get('origin') == tick and flow.get('started_at_zero'))
        completed = flow.get('completed_windows') if valid else None
        if self.pending is not None and self.pending['day'] == day - 1 and completed is not None:
            if tuple(flow['products']) != PRODUCTS or len(completed) != 3 or any(len(row) != 7 for row in completed):
                raise ValueError('Goods feedback requires3 horizons and7 products')
            gains = []
            for hi, row in enumerate(completed):
                values = []
                for pi, amount in enumerate(row):
                    if amount is None:
                        values.append(0.)
                    else:
                        if not isinstance(amount, int) or amount < 0:
                            raise ValueError('Certified goods feedback must be a nonnegative integer')
                        old = abs(self.pending['old'][hi][pi] - amount)
                        new = abs(self.pending['new'][hi][pi] - amount)
                        values.append((old - new) / self.model['absolute_loss_scale'])
                        self.scored_cells += 1
                gains.append(tuple(values))
            self.losses.append((day - 1, tuple(gains)))
        self.losses = [(when, gain) for when, gain in self.losses if when >= day - self.model['history_days']]
        self.pending = None
        self.day = day

    def weights(self, day):
        prior = math.log(self.model['prior'] / (1 - self.model['prior']))
        output = []
        for hi in range(3):
            values = []
            for pi in range(7):
                logit = prior + sum(gain[hi][pi] for when, gain in self.losses if day - self.model['history_days'] <= when < day)
                values.append(1 / (1 + math.exp(-min(40., max(-40., logit)))))
            output.append(tuple(values))
        return tuple(output)

    def remember(self, quantity, day):
        if self.day != day:
            raise ValueError('Observe the actual dawn before remembering its goods forecast')
        if quantity is not None and quantity.get('adaptive_goods_status') == 'applied':
            pair = quantity['adaptive_goods_experts']
            self.pending = dict(day=day, old=pair['old'], new=pair['new'])

    def state(self):
        return dict(day=self.day, pending=self.pending, losses=self.losses, scored_cells=self.scored_cells)


def apply(controller, quantity, stock, observation, day):
    if quantity is None:
        return
    if (int(observation['step']) % 24 or stock is None or stock['origin'] != int(observation['step'])
            or quantity.get('goods_reference_status') != 'applied'):
        quantity['adaptive_goods_status'] = 'unavailable_reference_or_stock'
        return
    if tuple(stock['products']) != PRODUCTS:
        raise ValueError('Stock posterior product order differs')
    original = quantity['point_estimate']
    difference = tuple(value - median for value, median in zip(stock['point_estimate'], stock['neighbor_median']))
    full = [[0.] * 7 for _ in range(3)]
    output = [list(row) for row in original]
    weights = controller.weights(day)
    for hi in range(3):
        for pi in range(7):
            cap = quantity['estimated_mature_goods_capacity'][pi]
            value = max(0., min(cap, original[hi][pi + 1] + controller.model['coefficient'][hi][pi] * difference[pi]))
            full[hi][pi] = max(value, full[hi - 1][pi]) if hi else value
            weight = weights[hi][pi]
            value = (1 - weight) * original[hi][pi + 1] + weight * full[hi][pi]
            output[hi][pi + 1] = min(cap, max(value, output[hi - 1][pi + 1])) if hi else min(cap, value)
    quantity['point_estimate'] = tuple(tuple(row) for row in output)
    quantity['adaptive_goods_status'] = 'applied'
    quantity['adaptive_goods_experts'] = dict(old=tuple(tuple(row[1:8]) for row in original), new=tuple(tuple(row) for row in full))
    quantity['adaptive_goods_weights'] = weights
    quantity['adaptive_goods_boundary'] = 'Public stock difference fromhistorical K8 stock;only certified completed prior-day sales adjustweights. Unidentified floor-price orown overflow transactions do not update. Hypothetical queries neverlearn.'
