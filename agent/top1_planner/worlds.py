"""Declared continuation hypotheses using only visible production assets."""
from collections import Counter
from copy import deepcopy

from . import rules
from .compiler import distance, nearest_shed
from .evaluator import MarketWorld


def public_worlds(observation, *, future_model='legacy', resource_loop=False):
    """Two bounded-size hypotheses, not bounds on all legal opponent futures.

    Existing visible farms keep receiving service. The optional resource loop
    renews visible wheat plots with funded seeds and balances grain/manure before
    sale, without crediting unobserved initial grain. It is disabled by default.
    No new fields or herds are inferred.
    Legacy shop sequences or mean demand from the
    uniform shop rule are explicit alternatives. Mean-demand value is not
    expected profit. Transportation delays differ as well.
    """
    start = int(observation['step'])
    if future_model not in ('legacy','uniform_mean'):
        raise ValueError('Unknown future demand model')
    other = 1-int(observation['player'])
    known = tuple(observation['town']['unlocked_shops'])
    diverse = tuple(rules.SHOPS)
    repeats = known or diverse
    worlds = []
    for index,delay in enumerate((4,12)):
        farm = deepcopy(observation['farms'][other])
        flow = {}
        public_farms={}
        def sell(tick,item,quantity):
            if quantity and start<=tick<719:
                batch = flow.setdefault(tick,{})
                batch[item] = batch.get(item,0)-quantity
        cargo=observation.get('_public_rival_cargo')
        if cargo:
            quantities=Counter(cargo['quantities'])
            if index:
                quantities.update(cargo.get('uncertain_upper',{}))
            for item,quantity in quantities.items():
                sell(cargo['origin']+delay,item,quantity)
        for day in range(start//24,30):
            public_farms[max(start,day*24)]=deepcopy(farm)
            for y,row in enumerate(farm['tiles']):
                for x,tile in enumerate(row):
                    if not isinstance(tile,dict):
                        continue
                    item = rules.ANIMALS[tile['animal']]['product'] if tile.get('animal') else tile.get('crop')
                    if not item:
                        continue
                    ready = bool(tile.get('animal')) or day-tile['planted_day']>=rules.CROPS[item]['first_yield_day']
                    if ready and tile.get('yield_units',0)>0:
                        tick = max(start,day*24)+max(delay,distance((x,y),nearest_shed((x,y)))+2)
                        sell(tick,item,tile['yield_units'])
                        tile['yield_units']=0
                        if item in rules.CROPS and not rules.CROPS[item]['ongoing']:
                            farm['tiles'][y][x]=None
                            continue
                    if tile.get('animal'):
                        tile['fed_today']=True
                        tile['cared_today']=True
                    else:
                        # WATER is an action, so its immediate crop yield is included.
                        if not tile.get('watered_today'):
                            tile['watered_today']=True
                            data = rules.CROPS[item]
                            if not data['ongoing'] and (data['max_yield_day']+1)//2<=day-tile['planted_day']<=data['max_yield_day']:
                                tile['yield_units']=min(data['max_yield'],tile['yield_units']+(2 if tile['fertilized_until_day']>=day else 1))
            rules._daily_refresh_plants(farm,day,24)
            rules._daily_refresh_animals(farm,day)
        sequence = repeats if index==0 else diverse
        shops = tuple(sequence[i%len(sequence)] for i in range(max(0,8-len(known))))
        consumption={}
        label='repeat_observed_shops' if index==0 else 'fixed_diverse_shop_sequence'
        if future_model=='uniform_mean':
            shops=()
            label='no_additional_shops_stress' if index==0 else 'uniform_mean_consumption_not_expected_profit'
            if index:
                average=Counter()
                for products in rules.SHOPS.values():
                    for product in products:
                        average[product]+=(2 if len(products)==1 else 1)/len(rules.SHOPS)
                for tick in range(start,719):
                    additional=min(8-len(known),tick//72-start//72)
                    if tick%4==0 and additional>0:
                        consumption[tick]={item:quantity*additional for item,quantity in average.items()}
        worlds.append(MarketWorld(future_shops=shops,external_flow=flow,public_farms=public_farms,
                                  additional_consumption=consumption,label=label,
                                  uncertain_shop_demand=future_model=='uniform_mean' and index==1))
    if resource_loop:
        from .resource_world import resource_loop_world
        worlds=[resource_loop_world(observation,world,delay=delay,initial_grain_credit=0)[0]
            for world,delay in zip(worlds,(4,12))]
    return tuple(worlds)
