"""Quote waiting across one public consumption event with paid work preserved."""
from collections import Counter
from copy import deepcopy
import time
from types import SimpleNamespace

from .early_release import _short_branch, _closure
from .sale_scenarios import SaleTimingScenario
from .ledger import rules


def _procurement_closed(baseline, alternative, seat):
    def receipts(branch):
        return Counter((row['step'], trade['operation'], trade['item'])
            for row in branch['rows'] for trade in row['transactions']
            if trade['seat']==seat and trade['operation']!='SELL'
            for _ in range(trade['quantity']))
    return receipts(baseline)==receipts(alternative)


def evaluate_consumption_wait(hypothesis, controller, observation, configuration,
                              action, belief, *, max_alternatives=6):
    """Only current public demand is used; future parent sales remain responsive.

    No future sale is forced. If the parent does not settle the delayed batch
    by the common short horizon, the physical-closure check rejects it.
    """
    started = time.perf_counter()
    tick, seat = int(observation['step']), int(observation['player'])
    end = tick+3
    result = dict(step=tick,end=end,changed=False,quotes=0,candidates=[],reason=None)
    if not hypothesis.consistent or hypothesis.transition is not None or controller.last_step!=tick:
        raise ValueError('Expected an observed source and the paid current own action')
    if tick%4 or end>719 or end//24!=tick//24:
        result['reason'] = 'No same-day post-consumption market window'
        return action,result
    demand = Counter()
    for shop in observation['town']['unlocked_shops']:
        products = rules.SHOPS[shop]
        for product in products:
            demand[product]+=2 if len(products)==1 else 1
    if tick%24==0:
        demand.update(rules.TOWN_CENTER_PRODUCTS)
    positions = {}
    for index,order in enumerate(action['market'][:10]):
        if len(order)>=3 and order[0]=='SELL' and order[1] not in ('WHEAT','FERTILIZER'):
            positions.setdefault(order[1],[]).append(index)
    candidates = []
    for item, indexes in positions.items():
        if len(indexes)!=1 or demand[item]<=0:
            continue
        index = indexes[0]
        maximum = min(action['market'][index][2],observation['private']['shed'].get(item,0))
        for quantity in dict.fromkeys((maximum,1)):
            if quantity<=0 or quantity>maximum:
                continue
            alternative = deepcopy(action)
            alternative['market'][index][2]-=quantity
            candidates.append((item,quantity,alternative))
    if not candidates:
        result['reason'] = 'No already stored batch sold into a known consumption event'
        return action,result
    baseline = _short_branch(hypothesis,controller,observation,configuration,action,belief,end)
    result['baseline_prediction'] = {k:baseline[k] for k in ('terminal','privates','rows')}
    result['quotes']+=1
    best,best_score = action,(0,0)
    for item,quantity,proposed in candidates[:max_alternatives]:
        alternative = _short_branch(hypothesis,controller,observation,configuration,proposed,belief,end)
        result['quotes']+=1
        closure = _closure(baseline,alternative,item)
        closure['same_successful_own_procurement'] = _procurement_closed(baseline,alternative,seat)
        deltas = [b['money']-a['money'] for a,b in zip(baseline['terminal']['farms'],alternative['terminal']['farms'])]
        own,relative = deltas[seat],deltas[seat]-deltas[1-seat]
        accepted = all(closure.values()) and own>=0 and relative>0
        row = dict(item=item,quantity=quantity,closure=closure,own_delta=own,relative_delta=relative,
            primary_accepted=accepted,scenarios=[],accepted=False)
        if accepted:
            for mode in ('omit_target_sales','delay_one_tick'):
                source = SimpleNamespace(private=hypothesis.private,
                    policy=SaleTimingScenario(hypothesis.policy,item,mode))
                base = _short_branch(source,controller,observation,configuration,action,belief,end)
                child = _short_branch(source,controller,observation,configuration,proposed,belief,end)
                result['quotes']+=2
                closed = _closure(base,child,item)
                closed['same_successful_own_procurement'] = _procurement_closed(base,child,seat)
                delta = [b['money']-a['money'] for a,b in zip(base['terminal']['farms'],child['terminal']['farms'])]
                safe = all(closed.values()) and delta[seat]>=0 and delta[seat]-delta[1-seat]>=0
                row['scenarios'].append(dict(mode=mode,closure=closed,own_delta=delta[seat],
                    relative_delta=delta[seat]-delta[1-seat],passed=safe))
                if not safe:
                    break
            row['accepted'] = len(row['scenarios'])==2 and all(s['passed'] for s in row['scenarios'])
        result['candidates'].append(row)
        if row['accepted'] and (relative,own)>best_score:
            best,best_score = proposed,(relative,own)
            result['selected'] = len(result['candidates'])-1
    result.update(changed=best is not action,own_delta=best_score[1],relative_delta=best_score[0],
        seconds=time.perf_counter()-started,reason='Paid short comparison across current public consumption')
    return best,result
