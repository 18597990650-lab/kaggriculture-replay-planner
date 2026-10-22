"""Compare a held warehouse batch with source-conditioned short continuations."""
from collections import Counter
from copy import deepcopy
import time

from .ledger import advance, clone_private, public_copy, rules


def _orders_for_release(action, item, quantity):
    child = deepcopy(action)
    orders = child['market']
    matches = [i for i, order in enumerate(orders)
               if len(order) >= 3 and order[:2] == ['SELL', item]]
    if len(matches) > 1 or len(orders) > 10:
        return None
    if matches:
        orders[matches[0]][2] += quantity
    else:
        if len(orders) == 10:
            return None
        index = 0
        while index < len(orders) and orders[index][0] == 'SELL':
            index += 1
        orders.insert(index, ['SELL', item, quantity])
    return child


def _short_branch(hypothesis, controller, observation, configuration, action, belief, end):
    seat = int(observation['player'])
    rival = 1-seat
    own, opponent = controller.fork(), hypothesis.policy.fork()
    supply = deepcopy(belief)
    current = public_copy(observation)
    privates = [None, None]
    privates[seat] = clone_private(observation['private'])
    privates[rival] = clone_private(hypothesis.private)
    rows = []
    for tick in range(int(observation['step']), end):
        own_obs = public_copy(current)
        own_obs.update(player=seat, private=clone_private(privates[seat]))
        if tick == observation['step']:
            own_action = deepcopy(action)
        else:
            own_obs['_public_rival_cargo'] = supply.observe(own_obs)
            own_action = own.action(own_obs)
        supply.remember_action(own_action)
        rival_obs = public_copy(current)
        rival_obs.update(player=rival, private=clone_private(privates[rival]))
        rival_action = opponent(rival_obs, configuration)
        actions = [own_action, rival_action] if seat == 0 else [rival_action, own_action]
        transition = advance(current, privates, actions, configuration)
        rows.append(dict(step=tick, own_action=own_action, rival_action=rival_action,
                         transactions=transition.transactions))
        current, privates = transition.public, transition.privates
    return dict(terminal=current, privates=privates, rows=rows, controller=own)


def _closure(baseline, alternative, item):
    same_farms = all({k:v for k,v in a.items() if k != 'money'} ==
                     {k:v for k,v in b.items() if k != 'money'}
                     for a,b in zip(baseline['terminal']['farms'], alternative['terminal']['farms']))
    same_private = baseline['privates'] == alternative['privates']
    same_market = baseline['terminal']['market'] == alternative['terminal']['market']
    same_work = all(a['own_action']['farmer'] == b['own_action']['farmer'] and
                    a['own_action']['hands'] == b['own_action']['hands'] and
                    [o for o in a['own_action']['market'] if o[:2] != ['SELL', item]] ==
                    [o for o in b['own_action']['market'] if o[:2] != ['SELL', item]]
                    for a,b in zip(baseline['rows'], alternative['rows']))
    a,b = baseline['controller'], alternative['controller']
    same_calendar = ((a.calendar is None and b.calendar is None) or
                     (a.calendar is not None and b.calendar is not None and
                      a.calendar.__dict__ == b.calendar.__dict__))
    same_execution = (a.programme == b.programme and a.day == b.day and
                      a.last_step == b.last_step and a.cursor == b.cursor and same_calendar
                      and a._tasks(a.day) == b._tasks(b.day))
    return dict(same_farms=same_farms, same_private=same_private, same_market=same_market,
                same_own_work_and_other_orders=same_work, same_execution=same_execution)


def evaluate_early_release(hypothesis, controller, observation, configuration, action,
                           belief, hold_record, *, advance_steps=2, max_alternatives=6):
    """Move only currently held sale goods, closing both ledgers after 1-2 steps.

    The caller has already generated its current action and observed the current
    public state. The source hypothesis has not consumed that state. No future
    replay actions, private labels, shop schedule or seed are accepted.
    """
    started = time.perf_counter()
    tick,seat = int(observation['step']), int(observation['player'])
    end = tick+advance_steps+1
    if advance_steps not in (1, 2) or max_alternatives < 1:
        raise ValueError('The short release experiment permits only one or two steps')
    if (not hypothesis.consistent or hypothesis.transition is not None or
            hypothesis.previous['step'] != tick or controller.last_step != tick):
        raise ValueError('Expected an observed source and a current paid own action')
    result = dict(step=tick, end=end, changed=False, quotes=0, candidates=[], reason=None)
    if not hold_record or hold_record['step'] != tick:
        result['reason'] = 'No current held batch'
        return action, result
    if end > 719 or end//24 != tick//24:
        result['reason'] = 'Unknown midnight weeds or a new-day decision lies in the window'
        return action, result
    sold = Counter()
    for order in action['market'][:10]:
        if len(order) >= 3 and order[0] == 'SELL':
            sold[order[1]] += order[2]
    available = {r['item']:min(r['quantity'], max(0, observation['private']['shed'].get(r['item'], 0)-sold[r['item']]))
                 for r in hold_record['held'] if r['item'] not in ('WHEAT', 'FERTILIZER')
                 and observation['market']['prices'][r['item']] > 1}
    available = {p:q for p,q in available.items() if q > 0}
    if not available:
        result['reason'] = 'No already stored non-reserved batch above the price floor'
        return action, result
    original_history = len(hypothesis.policy.history)
    private_before = clone_private(hypothesis.private)
    baseline = _short_branch(hypothesis, controller, observation, configuration, action, belief, end)
    result['quotes'] += 1
    result['baseline_prediction'] = {k:baseline[k] for k in ('terminal', 'privates', 'rows')}
    future_sales, rival_sales = Counter(), Counter()
    for row in baseline['rows']:
        for trade in row['transactions']:
            if trade['operation'] != 'SELL':
                continue
            if trade['seat'] == seat and row['step'] > tick:
                future_sales[trade['item']] += trade['quantity']
            elif trade['seat'] == 1-seat:
                rival_sales[trade['item']] += trade['quantity']
    candidates = []
    for item, held in available.items():
        quantity = min(held, future_sales[item])
        if quantity <= 0 or not rival_sales[item]:
            continue
        for count in dict.fromkeys((quantity, 1)):
            proposed = _orders_for_release(action, item, count)
            if proposed is not None:
                candidates.append((item, count, proposed))
    best, best_score = action, (0, 0)
    for item,count,proposed in candidates[:max_alternatives]:
        other = _short_branch(hypothesis, controller, observation, configuration, proposed, belief, end)
        result['quotes'] += 1
        closed = _closure(baseline, other, item)
        delta = [b['money']-a['money'] for a,b in zip(baseline['terminal']['farms'], other['terminal']['farms'])]
        own_delta, relative_delta = delta[seat], delta[seat]-delta[1-seat]
        accepted = all(closed.values()) and own_delta >= 0 and relative_delta > 0
        record = dict(item=item, quantity=count, closure=closed, own_cash_delta=own_delta,
                      relative_cash_delta=relative_delta, accepted=accepted,
                      parent_orders=action['market'], proposed_orders=proposed['market'],
                      baseline_transactions=baseline['rows'], alternative_transactions=other['rows'])
        result['candidates'].append(record)
        score = (relative_delta, own_delta)
        if accepted and score > best_score:
            best, best_score = proposed, score
            result['selected'] = len(result['candidates'])-1
    assert len(hypothesis.policy.history) == original_history
    assert hypothesis.private == private_before and hypothesis.transition is None
    result.update(changed=best is not action, seconds=time.perf_counter()-started,
                  reason='Short source-conditioned branch comparison',
                  available_held=available, baseline_future_sales=dict(future_sales),
                  baseline_rival_sales=dict(rival_sales), own_cash_delta=best_score[1],
                  relative_cash_delta=best_score[0])
    return best, result
