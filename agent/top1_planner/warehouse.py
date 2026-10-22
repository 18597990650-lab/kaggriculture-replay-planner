"""Forecast the parent's observed-stock night closure on a candidate calendar."""
from copy import deepcopy
from .material import actor_shadow

PRODUCTS=('WHEAT','CARROT','TOMATO','STRAWBERRY','MELON','EGG','MILK','WOOL','FERTILIZER')


def close(own, action, tape, cfg, rules):
    step=int(own['step']); day=step//24
    if step%24!=23 or not 12<=day<=28:
        return action
    if any(order and order[0]!='SELL' for order in action['market']):
        return action
    preview=deepcopy(own)
    actor_shadow(preview,action,cfg,rules)
    private=preview['private']; post=dict(private['shed'])
    for order in action['market']:
        if len(order)>=3 and order[0]=='SELL':
            post[order[1]]=max(0,post.get(order[1],0)-max(0,int(order[2])))
    needed=sum(post.values())+sum(max(0,q) for bag in private['inventories'] for q in bag.values())-100
    if needed<=0:
        return action
    result=deepcopy(action); orders=result['market']
    for item in sorted((p for p in PRODUCTS if p not in ('WHEAT','FERTILIZER')),
                       key=lambda p:-own['market']['prices'].get(p,0)):
        quantity=min(needed,post.get(item,0))
        if not quantity:
            continue
        existing=next((order for order in orders if len(order)>=3 and order[:2]==['SELL',item]),None)
        if existing is not None:
            existing[2]=max(0,int(existing[2]))+quantity
        elif len(orders)<10:
            orders.append(['SELL',item,quantity])
        else:
            continue
        needed-=quantity; post[item]-=quantity
        if needed<=0:
            break
    if needed>0:
        reserve=0
        for tick in range(step+1,719):
            future=tape[tick]
            for command in [future['farmer'],*future['hands']]:
                if command[:2]==['PICKUP','WHEAT']:
                    reserve+=max(0,int(command[2]) if len(command)>2 else 1)
            if any(order[:2]==['BUY_PRODUCT','WHEAT'] for order in future['market']):
                break
        incoming=sum(max(0,bag.get('WHEAT',0)) for bag in private['inventories'])
        others=sum(q for p,q in post.items() if p!='WHEAT')+sum(max(0,q) for bag in private['inventories'] for p,q in bag.items() if p!='WHEAT')
        quantity=min(needed,post.get('WHEAT',0),max(0,post.get('WHEAT',0)+incoming-reserve)) if 100-others>=reserve else 0
        existing=next((order for order in orders if len(order)>=3 and order[:2]==['SELL','WHEAT']),None)
        if quantity and (existing is not None or len(orders)<10):
            if existing is not None:
                existing[2]=max(0,int(existing[2]))+quantity
            else:
                orders.append(['SELL','WHEAT',quantity])
    return result
