"""Public-rule upper limits on non-purchasable goods entering the market."""
from functools import lru_cache
import math
from top1_planner import rules

PRODUCTS=tuple(rules.PRODUCTS)
PURCHASABLE=frozenset(('WHEAT','FERTILIZER'))
PARAMETER_KEYS=('base','I0','T','below_func','below_target','above_func','above_target')


@lru_cache(maxsize=256)
def floor_inventory(item,signature):
    parameters=dict(zip(PARAMETER_KEYS,signature))
    low=int(parameters['I0'])
    data={item:parameters}
    if rules.market_price(item,low,data)<=1:
        raise ValueError('Unsupported base price at or below the sale floor')
    distance=1
    while rules.market_price(item,low+distance,data)>1:
        distance*=2
        if distance>2**64:
            raise ValueError('Unable to bracket finite floor inventory')
    high=low+distance
    while low+1<high:
        midpoint=(low+high)//2
        if rules.market_price(item,midpoint,data)>1:
            low=midpoint
        else:
            high=midpoint
    assert rules.market_price(item,high,data)==1 and rules.market_price(item,high-1,data)>1
    return high


def upper_limits(observation,horizons=(4,8,24)):
    step=int(observation['step'])
    if step%24 or any(h<1 or h>24 for h in horizons):
        raise ValueError('Inventory limits require dawn windows within the known current day')
    market=observation['market']
    parameters=market.get('params')
    if parameters is None:
        parameters=rules.MARKET_PARAMS
    consumption={p:0 for p in PRODUCTS}
    for shop in observation['town']['unlocked_shops']:
        goods=rules.SHOPS[shop]
        for item in goods:
            consumption[item]+=2 if len(goods)==1 else 1
    result=[[None]*len(PRODUCTS) for _ in horizons]
    for index,item in enumerate(PRODUCTS):
        if item in PURCHASABLE:
            continue
        floor=floor_inventory(item,tuple(parameters[item][key] for key in PARAMETER_KEYS))
        # Both players quote before either commits, allowing one unit of overshoot.
        room=floor+1-market['inventory'][item]
        for hi,horizon in enumerate(horizons):
            ticks=range(step,step+horizon-1)
            consumed=sum(t%4==0 for t in ticks)*consumption[item]
            if item in rules.TOWN_CENTER_PRODUCTS:
                consumed+=sum(t%24==0 for t in ticks)
            result[hi][index]=max(0,math.floor(room+consumed))
    return result


def constrain_inventory(head,observation):
    if head is None:
        return
    if head['target']!='rival_market_inventory_contribution':
        raise ValueError('Inventory limits cannot be applied to gross trade quantities')
    caps=upper_limits(observation,tuple(head['horizons']))
    for hi,horizon in enumerate(head['probabilities']):
        for p,item in enumerate(PRODUCTS):
            if item in PURCHASABLE:
                continue
            for ti,threshold in enumerate(head['thresholds']):
                horizon[1][ti][p]=0.
                if caps[hi][p]<threshold:
                    horizon[0][ti][p]=0.
    head['public_inventory_upper_limits']=caps


def constrain_trades(head):
    if head is None:
        return
    if head['target']!='successful_sell_minus_successful_buy':
        raise ValueError('Trade constraints require net successful trade quantities')
    for horizon in head['probabilities']:
        for p,item in enumerate(PRODUCTS):
            if item not in PURCHASABLE:
                for values in horizon[1]:
                    values[p]=0.
