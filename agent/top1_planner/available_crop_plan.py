"""Generate complete crop cohorts on unoccupied, already owned land."""
from dataclasses import replace

from . import rules
from .compiler import distance
from .programmes import ProductionProgramme,Task


def available_crop_variants(programme,observation,controller,*,crop,sizes=(1,2)):
    """Keep paid assets, pending establishment, later reservations, and labor.

    Existing obsolete crop-service visits carry the new cohort. Missing visits
    are inserted as explicit tasks, so their travel and work enter full quotes.
    Empty-land preparation is performed by the native PLANT executor.
    """
    tick=int(observation['step'])
    if tick%24:
        raise ValueError('Available crop cohorts require a day boundary')
    start=tick//24
    data=rules.CROPS[crop]
    final_age=(data['first_yield_day']+(data['max_yield']-1)*data['interval']
               if data['ongoing'] else data['max_yield_day'])
    end=start+final_age+1
    if end>29:
        return ()
    farm=observation['farms'][int(observation['player'])]
    pending=controller.calendar.pending if controller.calendar is not None else {}
    points=[]
    for y,row in enumerate(farm['tiles']):
        for x,tile in enumerate(row):
            point=(x,y)
            if tile is not None and not (isinstance(tile,dict) and tile.get('kind')=='WEED'):
                continue
            if point in pending:
                continue
            tasks=[(day,t) for day in range(start,end) for seq in programme.days[day].values()
                   for t in seq if t.point==point]
            if any(t.operation in ('PLANT','PLACE','DIG','BUILD_COOP','BUILD_PASTURE') for day,t in tasks):
                continue
            covered=len({day for day,t in tasks if t.operation=='WATER'})
            near=min(distance(point,p) for p in ((4,4),(4,5),(5,4),(5,5)))
            points.append((end-start-covered,near,point))
    points.sort()
    candidates=[]
    seen=set()
    for count in sizes:
        selected=tuple(p for missing,near,p in points[:count])
        if not selected or selected in seen:
            continue
        seen.add(selected)
        days=[]
        for day,actors in enumerate(programme.days):
            if not start<=day<end:
                days.append(dict(actors))
                continue
            age=day-start
            productive=any(age+offset+1>=data['first_yield_day'] and (
                ((age+offset+1-data['first_yield_day'])%data['interval']==0 and
                 (age+offset+1-data['first_yield_day'])//data['interval']<data['max_yield'])
                if data['ongoing'] else age+offset<=data['max_yield_day']) for offset in range(3))
            rows={}
            visited=set()
            for actor,sequence in actors.items():
                revised=[]
                for task in sequence:
                    if task.point not in selected or task.operation not in ('WATER','FERTILIZE','HARVEST'):
                        revised.append(task)
                        continue
                    point=task.point
                    if point not in visited:
                        visited.add(point)
                        if day==start:
                            revised.append(Task(point,'PLANT',crop,start))
                        revised.append(Task(point,'WATER',crop,start))
                        if age>=data['first_yield_day'] and (data['ongoing'] or age==final_age):
                            revised.append(Task(point,'HARVEST',crop,start))
                    if task.operation=='FERTILIZE' and productive:
                        revised.append(replace(task,item=crop,generation=start))
                rows[actor]=tuple(revised)
            for point in selected:
                if point in visited:
                    continue
                visit=[]
                if day==start:
                    visit.append(Task(point,'PLANT',crop,start))
                visit.append(Task(point,'WATER',crop,start))
                if age>=data['first_yield_day'] and (data['ongoing'] or age==final_age):
                    visit.append(Task(point,'HARVEST',crop,start))
                choices=[]
                for actor in range(programme.workers[day]+1):
                    seq=rows.get(actor,())
                    route=((4,4),*(t.point for t in seq))
                    load=len(seq)+sum(distance(a,b) for a,b in zip(route,route[1:]))
                    for index in range(len(seq)+1):
                        before=route[index]
                        after=seq[index].point if index<len(seq) else None
                        if after==before:
                            continue
                        extra=distance(before,point)+len(visit)
                        if after is not None:
                            extra+=distance(point,after)-distance(before,after)
                        choices.append((max(0,load+extra-24),extra,load,actor,index))
                *_,actor,index=min(choices)
                seq=rows.get(actor,())
                rows[actor]=seq[:index]+tuple(visit)+seq[index:]
            days.append(rows)
        candidate=ProductionProgramme(programme.name+'-available-'+crop.lower()+'-'+str(start)+'-'+str(len(selected)),
            tuple(days),programme.workers)
        assert candidate.days[:start]==programme.days[:start]
        assert candidate.days[end:]==programme.days[end:]
        assert candidate.workers==programme.workers
        for day in range(start,end):
            for actor,seq in programme.days[day].items():
                preserved=[t for t in seq if t.point not in selected or t.operation not in ('WATER','FERTILIZE','HARVEST')]
                iterator=iter(candidate.days[day][actor])
                assert all(any(t==other for other in iterator) for t in preserved)
        candidates.append(candidate)
    return tuple(candidates)
