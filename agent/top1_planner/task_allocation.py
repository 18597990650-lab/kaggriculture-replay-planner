"""Value complete crop lots and revise the same work queues used for execution."""
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from itertools import combinations

from . import rules
from .compiler import distance, nearest_shed
from .programmes import ProductionProgramme, Task

CROP_WORK = {'PLANT','WATER','FERTILIZE','HARVEST','DIG','CLEAR_CROP'}


@dataclass(frozen=True)
class Revision:
    programme: ProductionProgramme
    detail: dict


class QuotePrices:
    """Marginal price path from the incumbent's declared native continuation."""

    def __init__(self, observation, compiled):
        self.start=int(observation['step'])
        self.params=observation['market'].get('params')
        self.inventory={self.start:dict(observation['market']['inventory'])}
        for row in compiled.result['records']:
            self.inventory[row['step']+1]=row['market_inventory']

    def __call__(self, item, step, quantity=1):
        if step>718:
            return 0
        inventory=self.inventory[max(self.start,int(step))][item]
        return sum(rules.market_price(item,inventory+i,self.params) for i in range(int(quantity)))


@lru_cache(maxsize=512)
def _crop_receipts(item,generation,start_day,paid_state):
    """Immutable biological yields shared by plots with the same crop state."""
    plant=dict(paid_state) if paid_state is not None else rules._new_plant(item,generation,24)
    farm=dict(tiles=[[plant]],farmer=[0,0],hands=[])
    private=dict(inventories=[{}],shed={},seeds={})
    crop=rules.CROPS[item]
    receipts=[]
    for day in range(max(start_day,generation),30):
        current=farm['tiles'][0][0]
        if not isinstance(current,dict) or current.get('crop')!=item:
            break
        rules._apply_unit_action(farm,private,0,['WATER'],1,day,24,100)
        age=day-generation
        harvest=(age>=crop['first_yield_day'] and
                 (crop['ongoing'] or current['yield_units']>=crop['max_yield'] or age>=crop['max_yield_day'] or day==29))
        if harvest:
            before=private['inventories'][0].get(item,0)
            rules._apply_unit_action(farm,private,0,['HARVEST'],1,day,24,100)
            quantity=private['inventories'][0].get(item,0)-before
            if quantity:
                receipts.append((day,quantity))
        rules._daily_refresh_plants(farm,day,24)
    return tuple(receipts)


def _crop_value(point, item, generation, start, price, tile=None):
    """Price biological yields at each plot's delivery time; paid seeds are sunk."""
    paid_state=None if tile is None else tuple(sorted(tile.items()))
    net=0 if tile is not None else -rules.CROPS[item]['seed']
    receipts=[]
    travel=distance(point,nearest_shed(point))+3
    for day,quantity in _crop_receipts(item,generation,start//24,paid_state):
        arrival=max(start,day*24)+travel
        if arrival<=718:
            cash=price(item,arrival,quantity)
            net+=cash
            receipts.append(dict(day=day,arrival=arrival,quantity=quantity,cash=cash))
    return net,receipts


@lru_cache(maxsize=256)
def _end_day(item,generation):
    data=rules.CROPS[item]
    if data['ongoing']:
        return generation+data['first_yield_day']+(data['max_yield']-1)*data['interval']
    farm=dict(tiles=[[rules._new_plant(item,generation,24)]],farmer=[0,0],hands=[])
    private=dict(inventories=[{}],shed={},seeds={})
    for age in range(data['max_yield_day']+1):
        rules._apply_unit_action(farm,private,0,['WATER'],1,generation+age,24,100)
        if age>=data['first_yield_day'] and farm['tiles'][0][0]['yield_units']>=data['max_yield']:
            return generation+age
        rules._daily_refresh_plants(farm,generation+age,24)
    return generation+data['max_yield_day']


class _Calendar:
    def __init__(self,programme,observation,controller,day_states=()):
        self.programme=programme
        self.obs=observation
        self.controller=controller
        self.start=int(observation['step'])
        self.day,self.hour=divmod(self.start,24)
        self.farm=observation['farms'][int(observation['player'])]
        self.farms={row['step']//24:row['farm'] for row in day_states}
        self.farms[self.day]=self.farm

    def rows(self):
        days=[dict(rows) for rows in self.programme.days]
        if self.controller.day==self.day and self.programme.days[self.day]==self.controller.programme.days[self.day]:
            days[self.day]=dict(self.controller._tasks(self.day))
        return days

    def cursor(self,day,actor):
        return self.controller.cursor.get(actor,0) if day==self.controller.day else 0

    def origin(self,day,actor):
        farm=self.farms.get(day,self.farm)
        if day!=self.day:
            farm=dict(farm,farmer=[4,4],hands=[])
        elif actor<=len(farm['hands']):
            return tuple(rules._farmer_position(farm,actor))
        else:
            farm=dict(farm,hands=list(farm['hands']))
        while actor>len(farm['hands']):
            farm['hands'].append(rules._spawn_hand(farm,10))
        return tuple(rules._farmer_position(farm,actor))

    def _events(self,remaining,day,establishing):
        """Keep queue indices while reducing each task to its route transition."""
        farm=self.farms.get(day)
        events=[]
        for task in remaining:
            point=task.point
            tile=farm['tiles'][point[1]][point[0]] if farm is not None else None
            obsolete=False
            if task.operation=='CLEAR_CROP' and farm is not None and not (
                    isinstance(tile,dict) and tile.get('crop')==task.item and tile.get('planted_day')==task.generation):
                obsolete=True
            if task.lot is None and farm is not None:
                if task.operation in ('WATER','FERTILIZE','HARVEST','FEED','CARE','COLLECT_FERTILIZER'):
                    if not isinstance(tile,dict) and point not in establishing:
                        obsolete=True
                    if isinstance(tile,dict):
                        if task.operation=='WATER' and tile.get('watered_today'):
                            obsolete=True
                        if task.operation=='FEED' and tile.get('fed_today'):
                            obsolete=True
                        if task.operation=='CARE' and tile.get('cared_today'):
                            obsolete=True
                if task.operation=='DIG' and tile is None:
                    obsolete=True
            if obsolete:
                events.append(None)
                continue
            if task.operation in ('DEPOSIT','DROP','PICKUP'):
                point=None
            work=1
            if task.operation=='PLANT' and isinstance(tile,dict) and tile.get('kind')=='WEED':
                work+=1
            events.append((point,work))
        return events

    @staticmethod
    def _advance(pos,event):
        if event is None:
            return pos,0
        point,work=event
        if point is None:
            point=nearest_shed(pos)
        return point,distance(pos,point)+work

    def duration(self,sequence,day,actor):
        remaining=sequence[self.cursor(day,actor):]
        establishing={t.point for t in remaining if t.operation in ('PLANT','PLACE')}
        pos=self.origin(day,actor)
        cost=0
        for event in self._events(remaining,day,establishing):
            pos,extra=self._advance(pos,event)
            cost+=extra
        return cost

    def available(self,day,actor):
        ticks=24-(self.hour if day==self.day else 0)
        farm=self.farm if day==self.day else dict(hands=[])
        if actor>len(farm['hands']):
            ticks-=1
        return min(ticks,719-max(day*24,self.start))

    def insert(self,rows,workers,day,block,allow_hire):
        old_count=max(workers[day],len(self.farm['hands']) if day==self.day else 0)
        upper=min(16,old_count+int(allow_hire and (day!=self.day or self.hour<16)))
        choices=[]
        for actor in range(upper+1):
            sequence=rows.get(actor,())
            previous=self.duration(sequence,day,actor)
            cursor=self.cursor(day,actor)
            remaining=sequence[cursor:]
            establishing={t.point for t in (*remaining,*block) if t.operation in ('PLANT','PLACE')}
            events=self._events(remaining,day,establishing)
            block_events=self._events(block,day,establishing)
            prefixes=[(0,self.origin(day,actor))]
            for event in events:
                cost,pos=prefixes[-1]
                pos,extra=self._advance(pos,event)
                prefixes.append((cost+extra,pos))
            tails={}
            def tail(index,pos):
                if index==len(events):
                    return 0
                key=(index,pos)
                if key not in tails:
                    after,extra=self._advance(pos,events[index])
                    tails[key]=extra+tail(index+1,after)
                return tails[key]
            for index in range(cursor,len(sequence)+1):
                if (index>cursor and index<len(sequence)
                        and sequence[index-1].point==sequence[index].point):
                    continue
                total,pos=prefixes[index-cursor]
                for event in block_events:
                    pos,extra=self._advance(pos,event)
                    total+=extra
                total+=tail(index-cursor,pos)
                capacity=self.available(day,actor)
                extra_over=max(0,total-capacity)-max(0,previous-capacity)
                wage=(rules._hire_cost(old_count,self.controller.cfg.get('farmHandCostMult',1))
                      if actor>old_count else 0)
                choices.append((extra_over,wage,total-previous,total,actor,index))
        if not choices:
            raise ValueError('No actor is available for a crop task bundle')
        over,wage,extra,total,actor,index=min(choices)
        sequence=rows.get(actor,())
        rows[actor]=sequence[:index]+block+sequence[index:]
        workers[day]=max(old_count,actor)
        return dict(day=day,actor=actor,insertion=index,additional_steps=extra,
            estimated_overload=over,marginal_wage=wage,origin=self.origin(day,actor))


def _resume_day(programme,point,after):
    return min((day for day in range(after+1,30) for seq in programme.days[day].values()
                for t in seq if t.point==point and t.operation in ('PLANT','PLACE')),default=30)


def _remove(days,calendar,point,start,stop):
    removed=[]
    for day in range(start,stop):
        for actor,sequence in list(days[day].items()):
            prefix=calendar.cursor(day,actor)
            keep=[]
            for index,task in enumerate(sequence):
                crop_task=task.operation in CROP_WORK and (task.item in rules.CROPS or task.operation=='DIG')
                if index>=prefix and task.point==point and crop_task:
                    removed.append(dict(day=day,actor=actor,operation=task.operation,item=task.item,generation=task.generation))
                else:
                    keep.append(task)
            days[day][actor]=tuple(keep)
    return removed


def _site_loss(programme,point,start,stop,observation,price):
    day=start//24
    farm=observation['farms'][int(observation['player'])]
    tile=farm['tiles'][point[1]][point[0]]
    loss=0
    paid=None
    if isinstance(tile,dict) and tile.get('crop'):
        paid=(tile['crop'],tile['planted_day'])
        loss+=_crop_value(point,*paid,start,price,tile)[0]
    future={(t.item,t.generation) for d in range(day,stop) for seq in programme.days[d].values()
            for t in seq if t.point==point and t.operation=='PLANT'}
    for item,generation in future:
        if generation<day or (paid is not None and item==paid[0] and paid[1]>=generation):
            continue
        loss+=_crop_value(point,item,generation,start,price)[0]
    return loss


def allocation_variants(programme,observation,controller,*,price,day_states=(),limit=4,timing='full_yield'):
    """Bound the shortlist, then build physically charged insertion/replacement lots.

    Current and future crop work may be displaced. Paid animals and establishment
    of animals during the new crop lifetime reserve their plots. Full quotes,
    rather than this screening estimate, decide admission.
    """
    calendar=_Calendar(programme,observation,controller,day_states)
    if timing not in ('full_yield','calendar_end'):
        raise ValueError('Unknown crop harvest timing policy')
    start,day=calendar.start,calendar.day
    proposals=[]
    for y,row in enumerate(calendar.farm['tiles']):
        for x,tile in enumerate(row):
            point=(x,y)
            if tile is not None and not (isinstance(tile,dict) and (tile.get('crop') or tile.get('kind')=='WEED')):
                continue
            for item in rules.CROPS:
                if isinstance(tile,dict) and tile.get('crop')==item:
                    continue
                biological_end=_end_day(item,day)
                if timing=='calendar_end' and not rules.CROPS[item]['ongoing']:
                    biological_end=day+rules.CROPS[item]['max_yield_day']
                end=min(29,biological_end)
                if day+rules.CROPS[item]['first_yield_day']>29 or end*24+distance(point,nearest_shed(point))+3>718:
                    continue
                stop=_resume_day(programme,point,end)
                if any(t.point==point and t.operation in ('PLACE','BUILD_COOP','BUILD_PASTURE')
                       for d in range(day,end+1) for seq in programme.days[d].values() for t in seq):
                    continue
                gross,receipts=_crop_value(point,item,day,start,price)
                loss=_site_loss(programme,point,start,stop,observation,price)
                near=min(distance(point,calendar.origin(day,a)) for a in range(max(len(calendar.farm['hands']),programme.workers[day])+1))
                proposals.append((gross-loss,near,point,item,end,stop,receipts,gross,loss))
    proposals.sort(key=lambda r:(-r[0],r[1],r[2],r[3]))
    results=[]
    for rank,near,point,item,end,stop,receipts,gross,loss in proposals[:limit]:
        for hire in (False,True):
            days=calendar.rows()
            workers=list(programme.workers)
            removed=_remove(days,calendar,point,day,stop)
            lot=f'{start}:{point[0]}:{point[1]}:{item}'
            scheduling=[]
            tile=calendar.farm['tiles'][point[1]][point[0]]
            for d in range(day,end+1):
                visit=[]
                if d==day:
                    if isinstance(tile,dict) and tile.get('crop'):
                        visit.append(Task(point,'CLEAR_CROP',tile['crop'],tile['planted_day'],lot))
                    visit.append(Task(point,'PLANT',item,day,lot))
                visit.append(Task(point,'WATER',item,day,lot))
                data=rules.CROPS[item]
                if d==end or (data['ongoing'] and d-day>=data['first_yield_day']):
                    visit.extend((Task(point,'HARVEST',item,day,lot),
                                  Task(nearest_shed(point),'DEPOSIT',item,None,lot)))
                scheduling.append(calendar.insert(days[d],workers,d,tuple(visit),hire))
            wage=sum(row['marginal_wage'] for row in scheduling)
            key=f'-allocate-{start}-{point[0]}-{point[1]}-{item.lower()}-'+('hire' if hire else 'paid')+'-'+timing
            candidate=ProductionProgramme(programme.name+key,tuple(days),tuple(workers))
            if results and results[-1].programme.days==candidate.days and results[-1].programme.workers==candidate.workers:
                continue
            results.append(Revision(candidate,dict(lot=lot,point=point,item=item,generation=day,end=end,
                resume_original=stop,harvest_timing=timing,mode='replacement' if removed or isinstance(tile,dict) and tile.get('crop') else 'insertion',
                paid_old_crop=None if not isinstance(tile,dict) or not tile.get('crop') else dict(item=tile['crop'],generation=tile['planted_day']),
                removed_tasks=removed,work=scheduling,marginal_wages=wage,estimate=gross-loss-wage,
                new_cash_estimate=gross,displaced_cash_estimate=loss,estimated_receipts=receipts,
                price_basis='Native incumbent future inventory; screening estimate, not admission',
                added_plantings=[dict(point=point,item=item,generation=day)])))
    return tuple(sorted(results,key=lambda r:(-r.detail['estimate'],r.programme.name)))


def _retimed_lots(programme,observation,controller,calendar):
    lots={}
    for seq in controller._tasks(calendar.day).values():
        for t in seq:
            if t.lot and t.item in rules.CROPS and t.operation in ('PLANT','WATER','FERTILIZE','HARVEST'):
                lots[t.lot]=t
    revisions=[]
    for lot,task in sorted(lots.items()):
        point,item=task.point,task.item
        tile=calendar.farm['tiles'][point[1]][point[0]]
        paid=isinstance(tile,dict) and tile.get('crop')==item and tile['planted_day']>=task.generation
        generation=tile['planted_day'] if paid else calendar.day
        if generation<=task.generation or (isinstance(tile,dict) and tile.get('animal')):
            continue
        if generation+rules.CROPS[item]['first_yield_day']>29:
            continue
        end=min(29,_end_day(item,generation))
        if any(t.point==point and t.operation in ('PLACE','BUILD_COOP','BUILD_PASTURE')
               for d in range(calendar.day,end+1) for seq in programme.days[d].values() for t in seq):
            continue
        stop=_resume_day(programme,point,end)
        days=calendar.rows()
        removed=_remove(days,calendar,point,calendar.day,stop)
        workers=list(programme.workers)
        scheduling=[]
        for day in range(calendar.day,end+1):
            visit=[]
            if day==calendar.day and not paid:
                if isinstance(tile,dict) and tile.get('crop'):
                    visit.append(Task(point,'CLEAR_CROP',tile['crop'],tile['planted_day'],lot))
                visit.append(Task(point,'PLANT',item,generation,lot))
            visit.append(Task(point,'WATER',item,generation,lot))
            if day==end or rules.CROPS[item]['ongoing'] and day-generation>=rules.CROPS[item]['first_yield_day']:
                visit.extend((Task(point,'HARVEST',item,generation,lot),Task(nearest_shed(point),'DEPOSIT',item,None,lot)))
            scheduling.append(calendar.insert(days[day],workers,day,tuple(visit),False))
        candidate=ProductionProgramme(programme.name+f'-retime-{calendar.start}-{point[0]}-{point[1]}',tuple(days),tuple(workers))
        assert controller.can_continue(candidate)
        revisions.append(Revision(candidate,dict(kind='actual_maturation_retime',step=calendar.start,
            point=point,item=item,lot=lot,previous_generation=task.generation,actual_generation=generation,
            paid=paid,end=end,resume_original=stop,work=scheduling,removed_tasks=removed,
            cancelled_points=[point] if not paid else [],lost_remaining_value=0)))
    return revisions


def delay_variants(programme,observation,controller,*,price,max_candidates=8):
    """Cancel the least remaining value that relieves an observed worker bottleneck.

    Only crop work competes here. Animal work, purchases and all executed queue
    prefixes remain intact. No paid input is refunded. A cancellation of wheat
    is priced at replacement purchase value and still uses ordinary feed orders.
    """
    calendar=_Calendar(programme,observation,controller)
    day=calendar.day
    if controller.day!=day:
        return ()
    rows=controller._tasks(day)
    retimed=_retimed_lots(programme,observation,controller,calendar)
    overloaded={a:calendar.duration(seq,day,a)-calendar.available(day,a) for a,seq in rows.items()}
    overloaded={a:n for a,n in overloaded.items() if n>0}
    overloaded={a:n for a,n in overloaded.items() if any(t.lot for t in rows[a][calendar.cursor(day,a):])}
    if not overloaded:
        return tuple(retimed)
    points={t.point for a in overloaded for t in rows[a][calendar.cursor(day,a):]
            if t.operation in CROP_WORK and t.item in rules.CROPS}
    choices=[]
    for point in points:
        tile=calendar.farm['tiles'][point[1]][point[0]]
        if isinstance(tile,dict) and tile.get('animal'):
            continue
        stop=_resume_day(programme,point,day)
        loss=_site_loss(programme,point,calendar.start,stop,observation,price)
        # Replacing lost feed has a purchase spread; do not count it as free.
        if isinstance(tile,dict) and tile.get('crop')=='WHEAT':
            loss=max(loss,rules.CROPS['WHEAT']['max_yield']*price('WHEAT',calendar.start,1))
        choices.append((loss,point,stop))
    choices.sort()
    candidates=[]
    for count in (1,2):
        for group in combinations(choices[:max_candidates],count):
            days=calendar.rows()
            removed=[]
            for loss,point,stop in group:
                removed.extend(_remove(days,calendar,point,day,stop))
            remaining={a:max(0,calendar.duration(days[day].get(a,()),day,a)-calendar.available(day,a)) for a in overloaded}
            if any(remaining.values()):
                continue
            loss=sum(row[0] for row in group)
            key=(loss,len(group),tuple(row[1] for row in group))
            candidates.append((key,group,days,removed,remaining))
    results=[]
    for key,group,days,removed,remaining in sorted(candidates,key=lambda row:row[0])[:3]:
        selected=[row[1] for row in group]
        label='-cancel-'+str(calendar.start)+'-'+'-'.join(f'{x}.{y}' for x,y in selected)
        candidate=ProductionProgramme(programme.name+label,tuple(days),programme.workers)
        assert controller.can_continue(candidate)
        results.append(Revision(candidate,dict(kind='delay_cancellation',step=calendar.start,cancelled_points=selected,
            lost_remaining_value=key[0],overload_before=overloaded,overload_after=remaining,
            removed_tasks=removed,original_workers=programme.workers[day],
            boundary='One- and two-crop subsets that relieve affected actors, ranked by remaining value. Full native continuations determine acceptance.')))
    # Transfer a crop visit to already paid labor before spending or abandoning it.
    transfers=[]
    for actor in overloaded:
        sequence=rows[actor]
        prefix=calendar.cursor(day,actor)
        for point in sorted({t.point for t in sequence[prefix:] if t.operation in CROP_WORK and t.item in rules.CROPS}):
            block=tuple(t for t in sequence[prefix:] if t.point==point and t.operation in CROP_WORK)
            if not block:
                continue
            source=sequence[:prefix]+tuple(t for t in sequence[prefix:] if t not in block)
            if calendar.duration(source,day,actor)>calendar.available(day,actor):
                continue
            product=next((t.item for t in block if t.operation=='HARVEST'),None)
            if product:
                block+= (Task(nearest_shed(point),'DEPOSIT',product,None,block[0].lot),)
            for other in range(len(calendar.farm['hands'])+1):
                if other==actor:
                    continue
                destination=rows.get(other,())
                for index in range(calendar.cursor(day,other),len(destination)+1):
                    modified=destination[:index]+block+destination[index:]
                    total=calendar.duration(modified,day,other)
                    if total>calendar.available(day,other):
                        continue
                    days=calendar.rows()
                    days[day][actor]=source
                    days[day][other]=modified
                    candidate=ProductionProgramme(programme.name+f'-transfer-{calendar.start}-{actor}-{other}-{point[0]}-{point[1]}-{index}',tuple(days),programme.workers)
                    assert controller.can_continue(candidate)
                    transfers.append((total,Revision(candidate,dict(kind='paid_labor_transfer',step=calendar.start,
                        point=point,source_actor=actor,destination_actor=other,insertion=index,
                        cancelled_points=[],lost_remaining_value=0,additional_hire=0))))
    return tuple(retimed)+tuple(r for _,r in sorted(transfers,key=lambda row:(row[0],row[1].programme.name))[:2])+tuple(results)


def allocation_event(observation,controller):
    """Public execution delay, sampled at most once per half day by the caller."""
    tick=int(observation['step'])
    if tick%6 or tick%24<6 or controller.day!=tick//24:
        return None
    calendar=_Calendar(controller.programme,observation,controller)
    events=[]
    for actor,sequence in controller._tasks(controller.day).items():
        remaining=sequence[controller.cursor.get(actor,0):]
        managed=[t for t in remaining if t.lot and t.operation in CROP_WORK]
        if not managed:
            continue
        duration=calendar.duration(sequence,controller.day,actor)
        capacity=calendar.available(controller.day,actor)
        delayed=[]
        shifted=False
        for task in managed:
            tile=calendar.farm['tiles'][task.point[1]][task.point[0]]
            moved=(task.operation!='CLEAR_CROP' and isinstance(tile,dict) and not tile.get('animal') and
                ((tile.get('crop')==task.item and tile.get('planted_day',-1)>task.generation)
                 or tile.get('crop')!=task.item and task.generation<calendar.day))
            shifted=shifted or moved
            if isinstance(tile,dict) and (moved or tile.get('kind')=='WEED' or
                    tile.get('crop')==task.item and (tile.get('planted_day',-1)>task.generation or
                    tile.get('consecutive_unwatered',0)>0)):
                delayed.append(dict(point=task.point,operation=task.operation,item=task.item,
                    generation=task.generation,actual=dict(tile)))
        if delayed and (duration>capacity or shifted):
            events.append(dict(actor=actor,estimated_steps=duration,available_steps=capacity,delayed=delayed))
    return dict(step=tick,actors=events) if events else None


def apply_revision(controller,revision):
    """Atomically inherit the already executed prefix and retire cancelled pending work."""
    child=controller.fork(programme=revision.programme)
    cancelled={tuple(p) for p in revision.detail.get('cancelled_points',())}
    if child.calendar is not None and cancelled:
        child.calendar.pending={p:job for p,job in child.calendar.pending.items() if p not in cancelled}
    child.allocation_records=list(controller.allocation_records)+[revision.detail]
    return child
