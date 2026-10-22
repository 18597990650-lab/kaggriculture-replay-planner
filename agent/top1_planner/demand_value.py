"""Reprice a settled trade tape over the exact marginal future-shop distribution."""
from collections import Counter,defaultdict
import time

from . import rules
from .settlement import _quotes
from .native_sale import _BACKEND
from .native_demand import reprice


def value_trade_tape(observation,configuration,unit_rounds,*,own_cash,rival_cash):
    """Hold accepted quantities and order rounds fixed; integrate unknown demand.

    Product marginals suffice for expected cash even though shop demands across
    products are correlated. This does not value future adaptive replanning or
    prove that a fixed tape remains funded in every demand realization.
    """
    begin=time.perf_counter()
    start=int(observation['step'])
    seat=int(observation['player'])
    known=tuple(observation['town']['unlocked_shops'])
    unlock=int(configuration['townShopUnlockInterval'])*24
    interval=int(configuration.get('townShopSellInterval',4))
    center=int(configuration.get('townCenterSellInterval',24))
    nodes=set(list(range((start//unlock+1)*unlock,719,unlock))[:max(0,8-len(known))])
    book=_quotes(observation['market'])
    per_item=defaultdict(dict)
    original=[0.0,0.0]
    for tick,rounds in unit_rounds.items():
        for accepted in rounds:
            by_item=defaultdict(list)
            for side,operation,item,price in accepted:
                sign=1 if operation=='SELL' else -1
                original[side]+=sign*price
                by_item[item].append((side,sign))
            for item,trades in by_item.items():
                per_item[item].setdefault(tick,[]).append(tuple(trades))
    expected=[0.0,0.0]
    products={}
    peak=1
    for item,events in per_item.items():
        rates=Counter((2 if len(goods)==1 else 1) if item in goods else 0 for goods in rules.SHOPS.values())
        probabilities=tuple((rate,count/len(rules.SHOPS)) for rate,count in rates.items())
        known_rate=sum((2 if len(rules.SHOPS[shop])==1 else 1) for shop in known if item in rules.SHOPS[shop])
        if _BACKEND == 'rust':
            earned, floor_units, terminal_states, item_peak = reprice(
                (observation['market'].get('params') or rules.MARKET_PARAMS)[item],
                observation['market']['inventory'][item], start, seat, known_rate, unlock,
                max(0, 8-len(known)), interval, center, item in rules.TOWN_CENTER_PRODUCTS, probabilities, events)
            expected[0]+=earned[0];expected[1]+=earned[1]
            peak=max(peak,item_peak)
            products[item]=dict(own_market=earned[seat],rival_market=earned[1-seat],
                expected_own_floor_sales=floor_units,terminal_states=terminal_states)
            continue
        states={(observation['market']['inventory'][item],0):1.0}
        earned=[0.0,0.0]
        quote=book[item].quote
        floor_units=0.0
        last=max(events)
        for tick in range(start,last+1):
            if tick in nodes:
                branches=defaultdict(float)
                for (inventory,rate),probability in states.items():
                    for addition,weight in probabilities:
                        branches[(inventory,rate+addition)]+=probability*weight
                states=branches
            for trades in events.get(tick,()):
                branches=defaultdict(float)
                for (inventory,rate),probability in states.items():
                    change=0
                    for side,sign in trades:
                        price=quote(inventory if sign==1 else inventory-1)
                        earned[side]+=probability*sign*price
                        if sign==1:
                            change+=price>1
                            if side==seat and price==1:
                                floor_units+=probability
                        else:
                            change-=1
                    branches[(inventory+change,rate)]+=probability
                states=branches
            if tick%interval==0 or tick%center==0:
                branches=defaultdict(float)
                for (inventory,rate),probability in states.items():
                    demand=(known_rate+rate if tick%interval==0 else 0)
                    demand+=int(tick%center==0 and item in rules.TOWN_CENTER_PRODUCTS)
                    branches[(inventory-demand,rate)]+=probability
                states=branches
            peak=max(peak,len(states))
        if abs(sum(states.values())-1.0)>1e-8:
            raise ValueError('Future-shop probability mass was lost')
        expected[0]+=earned[0];expected[1]+=earned[1]
        products[item]=dict(own_market=earned[seat],rival_market=earned[1-seat],
            expected_own_floor_sales=floor_units,terminal_states=len(states))
    own=own_cash+expected[seat]-original[seat]
    rival=rival_cash+expected[1-seat]-original[1-seat]
    return dict(own_cash=own,rival_market=rival,relative=own-rival,
        raw_own_cash=own_cash,raw_rival_market=rival_cash,
        own_correction=expected[seat]-original[seat],rival_correction=expected[1-seat]-original[1-seat],
        products=products,future_shop_nodes=len(nodes),peak_states=peak,seconds=time.perf_counter()-begin,
        basis='Exact marginal future-shop demand for fixed accepted trade volumes and lockstep rounds; no adaptive or all-world solvency guarantee')
