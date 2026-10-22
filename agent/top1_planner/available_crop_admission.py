"""Price one additional cohort after the original proposal frontier completes."""
from collections import Counter
import time

from . import rules
from .available_crop_plan import available_crop_variants
from .expanded_crop_plan import with_output_deliveries


def cash_value(data):
    """Use the same monetary basis in coarse ranking and complete admission."""
    demand=data.get('demand_value')
    return ((demand['own_cash'],demand['rival_market']) if demand is not None
            else (data['cash'],data['rival_net_cash']))


def quote_summary(data,observation):
    farm=observation['farms'][int(observation['player'])]
    paid={(x,y):(tile['animal'],tile['placed_day']) for y,row in enumerate(farm['tiles'])
          for x,tile in enumerate(row) if isinstance(tile,dict) and tile.get('animal')}
    missing={}
    for state in data['day_states']:
        for point,identity in paid.items():
            tile=state['farm']['tiles'][point[1]][point[0]]
            actual=(tile.get('animal'),tile.get('placed_day')) if isinstance(tile,dict) else None
            if actual!=identity:
                missing.setdefault(point,state['step'])
    demand=data.get('demand_value')
    own,rival=cash_value(data)
    summary=dict(own=own,rival=rival,relative=own-rival,
        raw_own=data['cash'],raw_rival=data['rival_net_cash'],
        value_basis='fixed_trade_expected_shop_demand' if demand is not None else 'declared_world',
        critical=Counter(i['code'] for i in data['issues'] if i['code'] in ('HIRE_UNFUNDED','FEED_SHORT','SEED_BATCH_SHORT')),
        missing=missing)
    if demand is not None:
        market={item:row['own_market'] for item,row in demand['products'].items()}
        summary['economy']=dict(product_market=market,
            outside_product_market=own-sum(market.values())-farm['money'],
            harvest=data['harvest'],sales_net=data['sales_net'],purchases_net=data['purchases_net'])
    return summary


def extend(observation,controller,programme,adaptive,world_indices,worlds,baseline,quote,deadline,estimated_full):
    """Incomplete quotes and inadequate remaining budget leave the old winner intact.

    Price-based priority schedules one proposal, never determines admission.
    Every completed world must preserve own cash and the old paid animal cohort;
    the minimum relative score must strictly improve on the old winner.
    """
    begin=time.perf_counter()
    proposals=[]
    for crop in ('MELON','TOMATO','STRAWBERRY','CARROT'):
        for proposed in available_crop_variants(programme,observation,controller,crop=crop,sizes=(2,)):
            candidate,meta=with_output_deliveries(programme,proposed,observation)
            count=len(meta['added_plantings'])
            data=rules.CROPS[crop]
            gross=count*(data['max_yield']*observation['market']['prices'][crop]-data['seed'])
            proposals.append((gross,crop,candidate,meta))
    record=dict(proposals=[dict(crop=crop,programme=p.name,priority_estimate=gross,plots=len(meta['added_plantings']))
        for gross,crop,p,meta in proposals],old_winner=programme.name,changed=False,quotes=0,
        selected=None,remaining_seconds=deadline-time.perf_counter())
    if not proposals:
        record['reason']='No available owned site outside paid, pending and future establishment obligations'
        return programme,record,None
    gross,crop,candidate,meta=max(proposals,key=lambda row:(row[0],row[1]))
    record['selected']=candidate.name
    required=len(world_indices)*estimated_full*1.15+0.10
    if time.perf_counter()+required>deadline:
        record.update(reason='The original frontier leaves insufficient budget for a complete expansion quote',required_seconds=required)
        return programme,record,None
    results=[]
    activated=[]
    for position,index in enumerate(world_indices):
        if time.perf_counter()+(len(world_indices)-position)*estimated_full*1.15+0.10>deadline:
            record.update(reason='Insufficient time to finish the same world set',seconds=time.perf_counter()-begin)
            return programme,record,None
        started=time.perf_counter()
        compiled=quote(candidate,adaptive,worlds[index],True)
        estimated_full=max(estimated_full,time.perf_counter()-started)
        record['quotes']+=1
        summary=quote_summary(compiled.result,observation)
        control=baseline[index]
        more=summary['critical']-control['critical']
        earlier={str(p):t for p,t in summary['missing'].items() if t<control['missing'].get(p,720)}
        for planting in meta['added_plantings']:
            x,y=planting['point']
            if any(isinstance(s['farm']['tiles'][y][x],dict)
                and s['farm']['tiles'][y][x].get('crop')==planting['item']
                and s['farm']['tiles'][y][x].get('planted_day',-1)>=planting['generation']
                for s in compiled.result['day_states']):
                activated.append((index,x,y))
        results.append(dict(world=index,own=summary['own'],rival=summary['rival'],
            relative=summary['relative'],own_delta=summary['own']-control['own'],
            relative_delta=summary['relative']-control['relative'],additional_critical=dict(more),
            earlier_paid_animal_loss=earlier,actual_new_plantings=sum(i==index for i,x,y in activated)))
    record.update(results=results,seconds=time.perf_counter()-begin)
    complete=time.perf_counter()<=deadline
    passed=complete and all(r['own_delta']>=0 and not r['additional_critical'] and not r['earlier_paid_animal_loss']
        and r['actual_new_plantings']>0 for r in results)
    passed=passed and min(r['relative'] for r in results)>min(baseline[i]['relative'] for i in world_indices)
    record.update(changed=bool(passed),completed_before_deadline=complete,
        reason='Complete expansion beats the original winner on the same worlds' if passed else 'Expansion admission rejected')
    return (candidate if passed else programme),record,results
