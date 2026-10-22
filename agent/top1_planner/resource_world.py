"""Declared existing-wheat cycles with grain and manure resource conservation."""
from collections import Counter
from dataclasses import replace

from . import rules
from .compiler import distance,nearest_shed


def _fertilizer_needed(tile,day):
    if not isinstance(tile,dict) or tile.get('crop')!='WHEAT':
        return False
    data=rules.CROPS['WHEAT']
    age=day-tile['planted_day']
    return (tile['yield_units']<data['max_yield'] and tile.get('fertilized_until_day',-1)<day
        and (data['max_yield_day']+1)//2<=age<=data['max_yield_day'])


def resource_loop_world(observation,world,*,delay,initial_grain_credit):
    """Retain visible wheat plots, feed existing herds, and sell only surplus.

    Grain credit is an explicit unknown-stock scenario, available only for feed;
    it is never sold as invented private stock. Replanting assumes funded seeds.
    Crop service, collection and delivery remain hypotheses, not rival actions
    recovered from the future. No new production plots or animals are invented.
    """
    start=int(observation['step'])
    if not 0<=start<719:
        raise ValueError('Resource-loop forecast entry must precede the last market')
    if not 0<=initial_grain_credit<=100:
        raise ValueError('Opening grain credit must fit the morning warehouse bound')
    farm=observation['farms'][1-int(observation['player'])]
    wheat={(x,y):dict(tile) for y,row in enumerate(farm['tiles']) for x,tile in enumerate(row)
        if isinstance(tile,dict) and tile.get('crop')=='WHEAT'}
    animals=[(x,y,tile) for y,row in enumerate(farm['tiles']) for x,tile in enumerate(row)
        if isinstance(tile,dict) and tile.get('animal')]
    flow={tick:{p:q for p,q in batch.items() if p not in ('WHEAT','FERTILIZER')}
        for tick,batch in world.external_flow.items()}
    flow={tick:batch for tick,batch in flow.items() if batch}
    public_farms={tick:dict(state,tiles=[list(row) for row in state['tiles']])
        for tick,state in world.public_farms.items()}
    stock=Counter()
    credit=int(initial_grain_credit)
    beginning=start//24
    ledger=[]
    locations=tuple(wheat)+tuple((x,y) for x,y,t in animals)
    travel=max((distance(p,nearest_shed(p))+2 for p in locations),default=2)
    arrival_delay=max(delay,travel)

    def trade(day,item,quantity):
        tick=max(start,day*24)+arrival_delay
        while quantity and tick<719:
            part=max(-80,min(80,quantity))
            batch=flow.setdefault(tick,{})
            batch[item]=batch.get(item,0)+part
            quantity-=part
            tick+=1
        return quantity

    for day in range(beginning,30):
        snapshot=public_farms[max(start,day*24)]
        for (x,y),tile in wheat.items():
            snapshot['tiles'][y][x]=dict(tile) if tile is not None else None
        initial=dict(stock)
        manure=sum(bool(t.get('fertilizer_available')) for x,y,t in animals) if day==beginning else len(animals)
        stock['FERTILIZER']+=manure
        harvest=0
        fertilizer=0
        replanted=0
        for point,tile in wheat.items():
            if not isinstance(tile,dict) or tile.get('crop')!='WHEAT':
                continue
            local=dict(tiles=[[tile]],farmer=[0,0],hands=[])
            private=dict(inventories=[{}],shed={},seeds={})
            if stock['FERTILIZER'] and _fertilizer_needed(tile,day):
                private['inventories'][0]['FERTILIZER']=1
                rules._apply_unit_action(local,private,0,['FERTILIZE'],1,day,24,100)
                stock['FERTILIZER']-=1
                fertilizer+=1
            rules._apply_unit_action(local,private,0,['WATER'],1,day,24,100)
            data=rules.CROPS['WHEAT']
            age=day-tile['planted_day']
            ready=age>=data['first_yield_day'] and (
                tile['yield_units']>=data['max_yield'] or age>=data['max_yield_day'] or day==29)
            if ready:
                rules._apply_unit_action(local,private,0,['HARVEST'],1,day,24,100)
                harvest+=private['inventories'][0].get('WHEAT',0)
                if day+data['first_yield_day']<=29:
                    local['tiles'][0][0]=rules._new_plant('WHEAT',day,24)
                    rules._apply_unit_action(local,private,0,['WATER'],1,day,24,100)
                    replanted+=1
            rules._daily_refresh_plants(local,day,24)
            wheat[point]=local['tiles'][0][0]
        stock['WHEAT']+=harvest
        feed=(sum(not t.get('fed_today') for x,y,t in animals) if day==beginning else len(animals)) if day<29 else 0
        credited=min(credit,feed)
        credit-=credited
        paid_feed=feed-credited
        purchase=max(0,paid_feed-stock['WHEAT'])
        stock['WHEAT']+=purchase-paid_feed
        if purchase:
            trade(day,'WHEAT',purchase)
        wheat_reserve=max(0,len(animals)-credit) if day<28 else 0
        wheat_sale=max(0,stock['WHEAT']-wheat_reserve)
        stock['WHEAT']-=wheat_sale
        withheld=trade(day,'WHEAT',-wheat_sale) if wheat_sale else 0
        fertilizer_reserve=max(0,sum(_fertilizer_needed(tile,day+1) for tile in wheat.values())-len(animals)) if day<29 else 0
        fertilizer_sale=max(0,stock['FERTILIZER']-fertilizer_reserve)
        stock['FERTILIZER']-=fertilizer_sale
        manure_withheld=trade(day,'FERTILIZER',-fertilizer_sale) if fertilizer_sale else 0
        ledger.append(dict(day=day,initial=initial,grain_harvest=harvest,manure_collected=manure,
            feed=feed,opening_credit_used=credited,grain_purchased=purchase,fertilizer_used=fertilizer,
            grain_sold=wheat_sale+withheld,manure_sold=fertilizer_sale+manure_withheld,
            unshipped_grain=-withheld,unshipped_manure=-manure_withheld,
            replanted_wheat=replanted,remaining=dict(stock),remaining_opening_credit=credit))
    replaced=replace(world,external_flow=flow,public_farms=public_farms,
        label=world.label+f'/visible_wheat_service_loop_grain_credit_{initial_grain_credit}')
    return replaced,tuple(ledger)
