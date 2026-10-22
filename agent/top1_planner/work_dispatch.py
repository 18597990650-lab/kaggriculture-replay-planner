"""Transfer ready visits between paid actors without changing establishment plans."""
from collections import Counter

from . import rules
from .compiler import distance,nearest_shed
from .programmes import Task
from .redeployment import identity,managed
from .service_cycle import service_required

SERVICE={'WATER','FERTILIZE','HARVEST','FEED','CARE','COLLECT_FERTILIZER'}


def _establishment_blocks(controller,task,farm):
    """Completed retry instructions do not reserve an already productive plot."""
    tile=farm['tiles'][task.point[1]][task.point[0]]
    if task.operation in ('PLANT','PLACE'):
        return not controller._established(task,tile)
    if task.operation=='CLEAR_CROP':
        return isinstance(tile,dict) and tile.get('crop')==task.item and tile.get('planted_day')==task.generation
    if task.operation=='DIG':
        return tile is not None and not (isinstance(tile,dict) and tile.get('animal'))
    if task.operation in ('BUILD_PASTURE','BUILD_COOP'):
        return not (isinstance(tile,dict) and tile.get('kind')==task.operation[6:])
    return False


def _ready(task,tile,day):
    if not isinstance(tile,dict):
        return False
    if not service_required(task,tile,day):
        return False
    if task.lot and identity(task,tile) is None:
        return False
    op=task.operation
    if op=='HARVEST':
        return bool(tile.get('yield_units')) and (bool(tile.get('animal')) or
            tile.get('crop') in rules.CROPS and day-tile['planted_day']>=rules.CROPS[tile['crop']]['first_yield_day'])
    if op=='WATER':
        return bool(tile.get('crop')) and not tile.get('watered_today')
    if op=='FERTILIZE':
        return bool(tile.get('crop')) and tile.get('fertilized_until_day',-1)<day+2
    if op=='FEED':
        return bool(tile.get('animal')) and not tile.get('fed_today')
    if op=='CARE':
        return bool(tile.get('animal')) and not tile.get('cared_today')
    return op=='COLLECT_FERTILIZER' and bool(tile.get('fertilizer_available'))


def take_paid_visit(controller,actor,farm,private,market,tick):
    """Borrow one complete ready visit only after the recipient's queue is empty.

    Every task remains owned by one queue. Seeds, animal placement, clearance,
    warehouse operations and the executed prefix are not transferable. Required
    feed and fertilizer must already be in the recipient's bag. Collection that
    the donor needs for a later use in the same trip remains with that donor.
    """
    day,hour=divmod(tick,24)
    rows=controller._tasks(day)
    recipient=rows.get(actor,())
    cursor=controller.cursor.get(actor,0)
    if cursor<len(recipient):
        return None
    available=min(24-hour,719-tick)
    if available<2:
        return None
    pos=tuple(rules._farmer_position(farm,actor))
    bag=private['inventories'][actor]
    successor_points={t.point for sequence in rows.values() for t in sequence
        if managed(t) and t.operation in SERVICE|{'PLANT','PLACE'}}
    establishing={t.point for owner,sequence in rows.items()
        for t in sequence[controller.cursor.get(owner,0):]
        if t.operation in ('PLANT','PLACE','DIG','CLEAR_CROP','BUILD_PASTURE','BUILD_COOP')
        and (t.point not in successor_points or _establishment_blocks(controller,t,farm))}
    candidates=[]
    for donor,sequence in rows.items():
        if donor==actor or donor>len(farm['hands']):
            continue
        start=controller.cursor.get(donor,0)
        previous=tuple(rules._farmer_position(farm,donor))
        eta=0
        index=start
        while index<len(sequence):
            task=sequence[index]
            point=task.point
            if task.operation not in SERVICE:
                eta+=distance(previous,point)+1
                previous=point
                index+=1
                continue
            end=index+1
            while end<len(sequence) and sequence[end].point==point and sequence[end].operation in SERVICE:
                end+=1
            block=sequence[index:end]
            tile=farm['tiles'][point[1]][point[0]]
            ready=[t for t in block if _ready(t,tile,day)
                and (not managed(t) or controller._successor_matches(t,tile))]
            travel=distance(previous,point)
            eta+=travel+len(ready)
            previous=point
            if not ready or point in establishing:
                index=end
                continue
            inputs=Counter('WHEAT' if t.operation=='FEED' else 'FERTILIZER'
                for t in ready if t.operation in ('FEED','FERTILIZE'))
            if any(bag.get(item,0)<qty for item,qty in inputs.items()):
                index=end
                continue
            product=rules.ANIMALS[tile['animal']]['product'] if tile.get('animal') else tile['crop']
            collected=Counter()
            if any(t.operation=='COLLECT_FERTILIZER' for t in ready):
                collected['FERTILIZER']+=1
            if any(t.operation=='HARVEST' for t in ready):
                collected[product]+=tile.get('yield_units',0)
            needed=controller._trip_inputs(donor,farm,day,end)
            if any(needed[item]>private['inventories'][donor].get(item,0)
                   for item in ('WHEAT','FERTILIZER') if collected[item]):
                index=end
                continue
            finish=distance(pos,point)+len(ready)
            delivery=distance(point,nearest_shed(point))+len(collected) if collected else 0
            if finish+delivery>available or finish>=eta:
                index=end
                continue
            # Prices rank completion opportunities; the full native policy
            # evaluation, rather than this local ranking, values final profit.
            value=sum(market['prices'][item]*qty for item,qty in collected.items())
            if any(t.operation in ('FEED','CARE','WATER') for t in ready):
                units=rules.ANIMALS[tile['animal']]['max_held'] if tile.get('animal') else rules.CROPS[product]['max_yield']
                value+=market['prices'][product]*units
            candidates.append((value/max(1,finish+delivery),eta-finish,-finish,donor,index,end,block,collected))
            index=end
    if not candidates:
        return None
    _,saved,negative_finish,donor,start,end,block,collected=max(candidates,key=lambda row:row[:6])
    changed=dict(rows)
    changed[donor]=rows[donor][:start]+rows[donor][end:]
    delivery=tuple(Task(nearest_shed(block[0].point),'DEPOSIT',item,None)
        for item in collected if collected[item])
    changed[actor]=recipient+block+delivery
    controller.day_sequences=changed
    if controller.calendar is not None:
        controller.calendar.sequences=dict(changed)
    return dict(step=tick,donor=donor,recipient=actor,point=block[0].point,
        operations=[t.operation for t in block],lots=[t.lot for t in block],
        estimated_steps_earlier=saved,estimated_finish=tick-negative_finish,
        carried_inputs=dict(bag),extra_hires=0,new_establishment=0)
