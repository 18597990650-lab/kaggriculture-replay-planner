"""Own complete-tail material simulation, derived from V644."""

from collections import Counter

from copy import deepcopy

from types import SimpleNamespace as NS

SHEDS={(4,4),(5,4),(4,5),(5,5)}

def clone_farm(farm):
    """Isolate mutable rule-owned fields; tile values are scalar in these rules."""
    return dict(farm,farmer=list(farm['farmer']),hands=[list(p) for p in farm['hands']],
                unlocked_quadrants=list(farm['unlocked_quadrants']),
                tiles=[[dict(tile) if isinstance(tile,dict) else tile for tile in row] for row in farm['tiles']])


def clone_action_farm(farm):
    """Borrow read-only tiles inside a private projection; writes must first copy."""
    return dict(farm,farmer=list(farm['farmer']),hands=[list(p) for p in farm['hands']],
                unlocked_quadrants=list(farm['unlocked_quadrants']),
                tiles=[list(row) for row in farm['tiles']])


def clone_private(private):
    return dict(private,shed=dict(private['shed']),seeds=dict(private['seeds']),
                inventories=[dict(bag) for bag in private['inventories']])


def clone_market(market):
    """Inventory and prices mutate; the configured price parameters are read-only."""
    return dict(market,inventory=dict(market['inventory']),prices=dict(market['prices']))


def units(action):return [action['farmer'],*action['hands']]

def actor_shadow(own,action,cfg,rules):
    """Apply the unit phase to the already isolated projection state."""
    seat=int(own['player']);farm=own['farms'][seat];private=own['private']
    errors=[];harvest=Counter()
    requested=Counter(op[1] for op in units(action) if op[:1]==['PLANT'])
    blocked={p for p,n in requested.items() if n>private['seeds'].get(p,0)}
    for i,op in enumerate(units(action)):
        positions=[farm['farmer'],*farm['hands']]
        if i>=len(positions):
            if op!=['PASS']:errors.append(dict(actor=i,code='ACTOR_ABSENT',op=op))
            continue
        x,y=positions[i];tile=farm['tiles'][y][x]
        bag=dict(private['inventories'][i]) if op==['HARVEST'] else private['inventories'][i];effective=op
        if op[:1]==['PLANT'] and op[1] in blocked:
            errors.append(dict(actor=i,code='SEED_BATCH_SHORT',op=op,point=[x,y]));effective=['PASS']
        if op==['FEED'] and isinstance(tile,dict) and tile.get('animal') and not tile['fed_today'] and bag.get('WHEAT',0)<1:
            errors.append(dict(actor=i,code='FEED_SHORT',op=op,point=[x,y]))
        if op==['FERTILIZE'] and isinstance(tile,dict) and tile.get('crop') and bag.get('FERTILIZER',0)<1:
            errors.append(dict(actor=i,code='FERTILIZER_SHORT',op=op,point=[x,y]))
        if op[:1]==['PICKUP'] and (x,y) in SHEDS and private['shed'].get(op[1],0)<(int(op[2]) if len(op)>2 else 1):
            errors.append(dict(actor=i,code='PICKUP_SHORT',op=op,point=[x,y]))
        rules._apply_unit_action(farm,private,i,effective,int(cfg['boardSize']),int(own['step'])//24,24,int(cfg['shedCapacity']))
        if op==['HARVEST']:
            for item,q in private['inventories'][i].items():
                if q>bag.get(item,0):harvest[item]+=q-bag.get(item,0)
    return errors,harvest

def market_shadow(own,action,cfg,rules):
    """Run the silent-rival market phase with only market-mutated fields copied."""
    seat=int(own['player']);source=own['farms'][seat]
    buys_land=any(o[:1]==['BUY_LAND'] for o in action['market'])
    farm=dict(source)
    farm['hands']=list(source['hands'])
    farm['unlocked_quadrants']=list(source['unlocked_quadrants'])
    farm['tiles']=deepcopy(source['tiles']) if buys_land else source['tiles']
    source_private=own['private'];private=dict(source_private)
    private['shed']=dict(source_private['shed']);private['seeds']=dict(source_private['seeds'])
    private['inventories']=list(source_private['inventories'])
    other=1-seat;farms=list(own['farms']);farms[seat]=farm
    rival=dict(shed={},seeds={},inventories=[{} for _ in [farms[other]['farmer'],*farms[other]['hands']]])
    privates=[private,rival] if seat==0 else [rival,private]
    source_market=own['market'];market=dict(source_market)
    market['inventory']=dict(source_market['inventory']);market['prices']=dict(source_market['prices'])
    states=[NS(action=action if i==seat else {},observation=NS(farms=farms,private=privates[i],market=market,town=own['town'])) for i in (0,1)]
    rules._process_market(states,NS(configuration=cfg))
    return farm,private,market,states

def project(obs,route,cfg,rules,*,fund=True,record=False,transform=None,end_step=719,generated_actions=False,fused_units=False):
    if generated_actions and (fund or transform is None):
        raise ValueError('Fresh action generation requires an unfunded transform')
    if fused_units and not generated_actions:
        raise ValueError('Unit-phase reuse requires generated actions')
    seat=int(obs['player']);own=deepcopy(obs);start=int(obs['step'])
    if not isinstance(end_step,int) or not start < end_step <= 719:
        raise ValueError('Projection end must follow its entry and precede terminal state')
    harvest=Counter();sales=Counter();purchases=Counter();issues=[];records=[];day_states=[];lost=Counter()
    low=float(own['farms'][seat]['money'])
    if fund:
        suffix=[Counter() for _ in range(721)]
        for t in range(719,-1,-1):
            suffix[t]=suffix[t+1].copy()
            suffix[t].update(op[1] for op in units(route[t]) if op[:1]==['PLANT'])
    for t in range(start,end_step):
        own.update(step=t,day=t//24,hour=t%24);funding=[]
        unit_phase=None
        completed_market=None
        if generated_actions:
            act=transform(own,None)
            if fused_units:
                act,unit_phase=act
        else:
            act=deepcopy(route[t])
            if transform is not None:act=transform(own,act)
        if unit_phase is None:
            err,h=actor_shadow(own,act,cfg,rules)
        else:
            own['farms'][seat],own['private'],err,h=unit_phase[:4]
            if len(unit_phase)==5:
                completed_market=unit_phase[4]
        if h:
            harvest.update(h)
        issues.extend(dict(step=t,**e) for e in err)
        f=own['farms'][seat];p=own['private']
        if fund:
            stock=Counter(p['seeds'])
            for slot,order in enumerate(act['market']):
                if len(order)>2 and order[0]=='BUY_SEED' and order[1] in ('WHEAT','CARROT'):
                    item=order[1];qty=min(order[2],max(0,suffix[t+1][item]-stock[item]));stock[item]+=qty
                    if qty!=order[2]:funding.append(dict(kind='SUBSET_UNUSED_UNPAID_SEEDS',step=t,slot=slot,item=item,before=order[2],after=qty));order[2]=qty
        before=dict(p['shed']);cashbefore=f['money'];hiresbefore=f['hires_today']
        if completed_market is None:
            nf,np,m,states=market_shadow(own,act,cfg,rules)
        else:
            nf,np,m,rounds=completed_market
            farms=list(own['farms']);farms[seat]=nf
            states=[NS(action=act if i==seat else {},observation=NS(farms=farms,
                private=np if i==seat else dict(shed={},seeds={},inventories=[]),market=m,town=own['town'])) for i in (0,1)]
            rules.reuse_own_market(states,cfg,before,rounds)
        def available(private):
            if t%24!=23:return private['shed'].get('WHEAT',0)
            shadow=deepcopy(private);rules._drop_inventories_to_shed(shadow,int(cfg['shedCapacity']))
            return shadow['shed'].get('WHEAT',0)
        if fund:
            end=min(719,t+2)
            if t%24==23 and t+1<719 and len(route[t+1]['market'])>=10:end=min(719,t+3)
            need=sum(int(op[2]) if len(op)>2 else 1 for a in route[t+1:end] for op in units(a) if op[:2]==['PICKUP','WHEAT'])
            have=available(np);missing=max(0,need-have)
            if missing:
                order=next((o for o in act['market'] if o[:2]==['BUY_PRODUCT','WHEAT']),None)
                if order is not None:order[2]+=missing
                elif len(act['market'])<int(cfg['maxMarketOrdersPerTurn']):act['market'].append(['BUY_PRODUCT','WHEAT',missing])
                else:
                    for o in act['market']:
                        if o[:2]==['SELL','WHEAT']:
                            saved=min(missing,max(0,int(o[2])));o[2]-=saved;missing-=saved
                            if not missing:break
                nf,np,m,states=market_shadow(own,act,cfg,rules)
                funding.append(dict(kind='SUBSET_FEED_RESERVE',step=t,required_by=end-1,requested=need,projected_available_before=have,
                    projected_available_after=available(np),source='Current observed warehouse plus declared deposits and settled current orders',
                    warehouse_purchase_does_not_fund_current_actor=True))
        f,p=nf,np
        for item in set(before)|set(p['shed']):
            n=p['shed'].get(item,0)-before.get(item,0)
            if n<0:sales[item]-=n
            if n>0:purchases[item]+=n
        req_hire=sum(o==['HIRE'] for o in act['market'])
        if f['hires_today']-hiresbefore!=req_hire:issues.append(dict(step=t,code='HIRE_UNFUNDED'))
        low=min(low,f['money']);rules._town_consume(NS(configuration=cfg),states,t)
        rules._decay_plants(f,t)
        if t%24==23:
            rules._daily_refresh_plants(f,t//24,24);rules._daily_refresh_animals(f,t//24)
            total=Counter(p['shed'])
            for bag in p['inventories']:total.update(bag)
            rules._drop_inventories_to_shed(p,int(cfg['shedCapacity']));lost.update(total-Counter(p['shed']))
            f['farmer']=[4,4];f['hands']=[];f['hires_today']=0;p['inventories']=[{}]
            day_states.append(dict(step=t+1,farm=clone_farm(f),private=clone_private(p)))
        own['farms'][seat]=f;own['private']=p;own['market']=m
        if record:records.append(dict(step=t,action=act,cash_before=cashbefore,cash_after=f['money'],shed=dict(p['shed']),harvest=dict(h),issues=err,funding=funding,market_inventory=dict(m['inventory'])))
    return dict(cash=own['farms'][seat]['money'],minimum_cash=low,harvest=dict(harvest),sales_net=dict(sales),purchases_net=dict(purchases),
        issues=issues,lost=dict(lost),day_states=day_states,final=own,records=records)
