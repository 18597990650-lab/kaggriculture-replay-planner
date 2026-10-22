"""Certify non-resource sales from public markets and already resolved own sales."""
from collections import Counter, deque
from top1_planner import rules

PRODUCTS = ('CARROT', 'TOMATO', 'STRAWBERRY', 'MELON', 'EGG', 'MILK', 'WOOL')


class GoodsFlowObserver:
    def __init__(self, configuration):
        self.configuration = {key: int(configuration[key]) for key in
            ('turnsPerDay', 'townShopSellInterval', 'townCenterSellInterval')}
        if self.configuration['turnsPerDay'] != 24:
            raise ValueError('Goods forecasts require a24-step day')
        self.previous = None
        self.history = deque(maxlen=24)
        self.started_at_zero = False
        self.completed_history = deque(maxlen=3)

    def observe(self, observation, own_sales, own_evidence):
        tick = int(observation['step'])
        values = (None,) * 7
        reasons = ('initial',) * 7
        if self.previous is None:
            self.started_at_zero = tick == 0
        else:
            old = self.previous
            if tick != old['step'] + 1:
                raise ValueError('Goods sales inference requires consecutive public observations')
            if own_evidence is None:
                raise RuntimeError('Resolve our preceding sale bound before observing goods flow')
            consumed = Counter()
            if old['step'] % self.configuration['townShopSellInterval'] == 0:
                for shop in old['shops']:
                    products = rules.SHOPS[shop]
                    for product in products:
                        consumed[product] += 2 if len(products) == 1 else 1
            if old['step'] % self.configuration['townCenterSellInterval'] == 0:
                consumed.update({product: 1 for product in rules.TOWN_CENTER_PRODUCTS})
            values, reasons = [], []
            for product in PRODUCTS:
                total = observation['market']['inventory'][product] - old['inventory'][product] + consumed[product]
                if total < 0:
                    raise ValueError('A non-purchasable good has negative market supply')
                peak = old['inventory'][product] + total
                if rules.market_price(product, peak, old['params']) <= 1:
                    values.append(None); reasons.append('floor_sales_unidentified')
                elif not own_evidence['exact'] and own_evidence['requested'].get(product, 0):
                    values.append(None); reasons.append('own_warehouse_overflow_ambiguous')
                else:
                    amount = total - own_sales.get(product, 0)
                    if amount < 0:
                        raise ValueError('Resolved own sales exceed public market additions')
                    values.append(amount); reasons.append('identified_public_market_and_own_fills')
            values, reasons = tuple(values), tuple(reasons)
            self.history.append(values)
        completed = None
        if tick % 24 == 0 and len(self.history) == 24:
            history = list(self.history)
            completed = tuple(tuple(sum(row[product] for row in history[:horizon])
                if all(row[product] is not None for row in history[:horizon]) else None
                for product in range(7)) for horizon in (4, 8, 24))
        if completed is not None:
            self.completed_history.append((tick//24-1,completed))
        self.previous = dict(step=tick, inventory=dict(observation['market']['inventory']),
            params=observation['market'].get('params'), shops=tuple(observation['town']['unlocked_shops']))
        return dict(origin=tick, started_at_zero=self.started_at_zero, products=PRODUCTS,
            last_transaction=values, reasons=reasons, completed_windows=completed, recent_completed=tuple(self.completed_history),
            evidence='Successful non-resource sales,certified only when market trades stay above theone-dollar floor and own fills are exact. Unknowns remain null.')
