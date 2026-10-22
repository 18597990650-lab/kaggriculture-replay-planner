"""Public cross-product sale cadence and current funding context."""
import math

BASE = (35, 60, 120, 250, 50, 160, 200)
TIMING_NAMES = ('total_goods_stock', 'other_stock_value', 'stock_value_share',
    'other_capacity_value', 'capacity_value_share', 'other_early1', 'other_early3',
    'other_late1', 'other_late3', 'other_known1', 'other_known3')
FUNDING_NAMES = ('grain', 'feeding_shortfall', 'cash_per_feed_bill', 'wheat_price',
    'grain_buy_last24', 'fertilizer_sell_last24', 'grain_flow_coverage',
    'fertilizer_flow_coverage', 'unknown_grain_trades', 'asset_growth3',
    'herd_growth1', 'crop_growth1', 'cash_change1')


def features(public, current, previous1, previous3, grain, flow, day):
    if len(public) != 7 or any(len(row) != 36 for row in public):
        raise ValueError('Seven public feature rows required')
    if grain['origin'] != day * 24 or flow['origin'] != day * 24:
        raise ValueError('Resource context does not match dawn')
    if not grain['started_at_zero'] or not flow['started_at_zero'] or flow['window_steps'] != 24:
        raise ValueError('Consecutive public resource accounting required')
    stock = [row[3] * 6 for row in public]
    capacity = [row[5] * 6 for row in public]
    prices = [row[6] * base for row, base in zip(public, BASE)]
    stock_value = [units * price for units, price in zip(stock, prices)]
    capacity_value = [units * price for units, price in zip(capacity, prices)]
    result = []
    for pi in range(7):
        groups = []
        for lags in (1, 3):
            early = late = total = known = 0.
            for pj in range(7):
                if pi == pj:
                    continue
                for lag in range(lags):
                    four, eight, daily = public[pj][27 + lag * 3:30 + lag * 3]
                    if min(four, eight, daily) < 0:
                        continue
                    if four > eight + 1e-8 or eight > daily + 1e-8:
                        raise ValueError('Public cumulative windows are not nested')
                    early += four * 6
                    late += (daily - eight) * 6
                    total += daily * 6
                    known += 1
            groups.append((early / (total + 1), late / (total + 1), known / (6 * lags)))
        animals = sum(current['assets'][5:8])
        other_stock = max(0., sum(stock_value) - stock_value[pi])
        other_capacity = max(0., sum(capacity_value) - capacity_value[pi])
        shortage = max(0., animals - grain['point'])
        values = (sum(stock) / 100, math.log1p(other_stock / 1000),
            stock_value[pi] / (1 + sum(stock_value)), math.log1p(other_capacity / 1000),
            capacity_value[pi] / (1 + sum(capacity_value)),
            groups[0][0], groups[1][0], groups[0][1], groups[1][1], groups[0][2], groups[1][2],
            math.log1p(grain['point'] / 6), shortage / 6,
            math.log1p(max(0., current['cash']) / (1 + current['prices'][0] * animals)),
            current['prices'][0] / 25, flow['known_buy_last24'][0] / 12,
            flow['known_sell_last24'][1] / 12, flow['observed_steps_last24'][0] / 24,
            flow['observed_steps_last24'][1] / 24, math.log1p(grain['unknown_trades']),
            (current['assets'][pi + 1] - previous3['assets'][pi + 1]) / 4,
            (animals - sum(previous1['assets'][5:8])) / 4,
            (sum(current['assets'][:5]) - sum(previous1['assets'][:5])) / 20,
            (current['cash'] - previous1['cash']) / 1000)
        if len(values) != len(TIMING_NAMES) + len(FUNDING_NAMES) or not all(math.isfinite(v) for v in values):
            raise ValueError('Invalid public context features')
        result.append(values)
    return tuple(result)
