"""Budgeted, physically closed postponement across current public consumption."""
from collections import Counter, deque
from copy import deepcopy
import time
from types import SimpleNamespace

from .early_release import _short_branch, _closure
from .consumption_window import _procurement_closed
from .sale_scenarios import SaleTimingScenario
from .concentrated_sale_scenario import ConcentratedSaleScenario
from .ledger import rules


def evaluate_consumption_wait(hypothesis, controller, observation, configuration,
                              action, belief, *, deadline, max_alternatives=4):
    started = time.perf_counter()
    tick, seat = int(observation['step']), int(observation['player'])
    end = tick + 3
    result = dict(step=tick, end=end, changed=False, quotes=0, candidates=[],
                  own_delta=0, relative_delta=0, budget_exhausted=False)
    if (not hypothesis.consistent or hypothesis.transition is not None or
            hypothesis.previous['step'] != tick or controller.last_step != tick):
        raise ValueError('Expected the current observed source and paid own action')
    if tick % 4 or end > 719 or end // 24 != tick // 24:
        result['reason'] = 'No same-day post-consumption market window'
        return action, result
    demand = Counter()
    for shop in observation['town']['unlocked_shops']:
        products = rules.SHOPS[shop]
        demand.update({p: 2 if len(products) == 1 else 1 for p in products})
    if tick % 24 == 0:
        demand.update(rules.TOWN_CENTER_PRODUCTS)
    positions = {}
    for index, order in enumerate(action['market'][:10]):
        if len(order) >= 3 and order[0] == 'SELL' and order[1] not in ('WHEAT', 'FERTILIZER'):
            positions.setdefault(order[1], []).append(index)
    candidates = []
    for item, indexes in positions.items():
        if len(indexes) != 1 or demand[item] <= 0:
            continue
        index = indexes[0]
        maximum = min(action['market'][index][2], observation['private']['shed'].get(item, 0))
        inventory = observation['market']['inventory'][item]
        params = observation['market'].get('params')
        recovery = rules.market_price(item, max(0, inventory-demand[item]), params)
        recovery -= rules.market_price(item, inventory, params)
        for quantity in dict.fromkeys((maximum, 1)):
            if quantity <= 0 or quantity > maximum:
                continue
            proposed = deepcopy(action)
            proposed['market'][index][2] -= quantity
            candidates.append((quantity*recovery, quantity, item, proposed))
    candidates.sort(key=lambda row: (-row[0], -row[1], row[2]))
    result['eligible_alternatives'] = len(candidates)
    if not candidates:
        result['reason'] = 'No already stored sale into current public demand'
        return action, result

    peak_quote = 0.035
    recent_quotes = deque([peak_quote], maxlen=2)

    def quote(source, orders):
        nonlocal peak_quote
        if time.perf_counter() + max(recent_quotes)*1.5 + 0.01 >= deadline:
            result['budget_exhausted'] = True
            return None
        clock = time.perf_counter()
        branch = _short_branch(source, controller, observation, configuration, orders, belief, end)
        elapsed = time.perf_counter()-clock
        peak_quote = max(peak_quote, elapsed)
        recent_quotes.append(elapsed)
        result['quotes'] += 1
        return branch

    def compare(base, child, item, *, strict):
        closed = _closure(base, child, item)
        closed['same_successful_own_procurement'] = _procurement_closed(base, child, seat)
        deltas = [b['money']-a['money'] for a, b in
                  zip(base['terminal']['farms'], child['terminal']['farms'])]
        own, relative = deltas[seat], deltas[seat]-deltas[1-seat]
        passed = all(closed.values()) and own >= 0 and (relative > 0 if strict else relative >= 0)
        return dict(closure=closed, own_delta=own, relative_delta=relative, passed=passed)

    baseline = quote(hypothesis, action)
    if baseline is None:
        result['reason'] = 'No budget for a complete branch'
        return action, result
    result['baseline_prediction'] = {k: baseline[k] for k in ('terminal', 'privates', 'rows')}
    proposed_by_index = {}
    for _, quantity, item, proposed in candidates[:max_alternatives]:
        child = quote(hypothesis, proposed)
        if child is None:
            break
        primary = compare(baseline, child, item, strict=True)
        row = dict(item=item, quantity=quantity, **primary,
                   primary_accepted=primary['passed'], scenarios=[], accepted=False)
        proposed_by_index[len(result['candidates'])] = proposed
        result['candidates'].append(row)
    ranking = sorted((i for i, row in enumerate(result['candidates']) if row['primary_accepted']),
        key=lambda i: (result['candidates'][i]['relative_delta'], result['candidates'][i]['own_delta']),
        reverse=True)
    baselines = {}
    chosen = action
    for index in ranking:
        row = result['candidates'][index]
        item = row['item']
        proposed = proposed_by_index[index]
        for mode in ('omit_target_sales', 'delay_one_tick', 'concentrated_recovery_sale'):
            policy = (ConcentratedSaleScenario(hypothesis.policy, item)
                if mode == 'concentrated_recovery_sale'
                else SaleTimingScenario(hypothesis.policy, item, mode))
            source = SimpleNamespace(private=hypothesis.private, policy=policy)
            key = (item, mode)
            if key not in baselines:
                base = quote(source, action)
                if base is None:
                    break
                baselines[key] = base
            child = quote(source, proposed)
            if child is None:
                break
            check = dict(mode=mode, **compare(baselines[key], child, item, strict=False))
            row['scenarios'].append(check)
            if not check['passed']:
                break
        row['accepted'] = (len(row['scenarios']) == 3 and
            all(check['passed'] for check in row['scenarios']) and time.perf_counter() < deadline)
        if row['accepted']:
            chosen = proposed
            result.update(changed=True, selected=index, own_delta=row['own_delta'],
                          relative_delta=row['relative_delta'])
            break
        if result['budget_exhausted']:
            break
    result.update(seconds=time.perf_counter()-started, peak_quote_seconds=peak_quote,
        reason='All physical and procurement checks plus three funded sale-error scenarios required')
    return chosen, result
