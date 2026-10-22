"""Offer complete continuations for paid animals facing a planned feed exit."""
from collections import Counter,defaultdict

from . import rules
from .compiler import nearest_shed
from .programmes import ProductionProgramme,Task
from .zone_transfer import _insert_tour,_length

SUPPLY={'PICKUP','DROP','DEPOSIT'}


def _planned_feed(programme,day,point):
    return any(task.point==point and task.operation=='FEED'
               for sequence in programme.days[day].values() for task in sequence)


def _production_day(animal,placed_day,day):
    data=rules.ANIMALS[animal]
    age=day-placed_day-data['first_yield_day']
    return age>=0 and age%data['interval']==0


def _planned_exits(programme,observation,horizon_days):
    day=int(observation['step'])//24
    farm=observation['farms'][int(observation['player'])]
    exits=[]
    for y,row in enumerate(farm['tiles']):
        for x,tile in enumerate(row):
            if not isinstance(tile,dict) or not tile.get('animal'):
                continue
            point=(x,y)
            consecutive=tile['consecutive_unfed']
            first_missing=None
            for future_day in range(day,min(30,day+horizon_days)):
                if _planned_feed(programme,future_day,point):
                    consecutive=0
                    first_missing=None
                else:
                    if first_missing is None:
                        first_missing=future_day
                    consecutive+=1
                if consecutive>=2:
                    start=max(day,first_missing)
                    animal=tile['animal']
                    productions=tuple(d for d in range(start+1,30) if _production_day(animal,tile['placed_day'],d))
                    if productions:
                        preferred=0
                        for d in range(day,min(30,start+1)):
                            for actor,seq in programme.days[d].items():
                                if any(t.point==point and t.operation in ('FEED','CARE','HARVEST','COLLECT_FERTILIZER') for t in seq):
                                    preferred=actor
                        exits.append(dict(point=point,animal=animal,placed_day=tile['placed_day'],start=start,
                                          entry_unfed=tile['consecutive_unfed'],preferred=preferred,productions=productions))
                    break
    return exits


def _retained_programme(parent,cohort,observation,cadence):
    day=int(observation['step'])//24
    consecutive={row['point']:row['entry_unfed'] for row in cohort}
    by_point={row['point']:row for row in cohort}
    days=list(parent.days[:day])
    for future_day in range(day,30):
        active={p:row for p,row in by_point.items() if future_day>=row['start']}
        original=parent.days[future_day]
        rows={actor:tuple(t for t in seq if t.point not in active or t.operation in SUPPLY)
              for actor,seq in original.items()}
        tours=defaultdict(list)
        for point,row in by_point.items():
            if point not in active:
                consecutive[point]=0 if _planned_feed(parent,future_day,point) else consecutive[point]+1
                continue
            animal=row['animal']
            generation=row['placed_day']
            future_production=any(d>future_day for d in row['productions'])
            feed=future_production and (cadence=='daily' or consecutive[point]>=1
                                        or _production_day(animal,generation,future_day+1))
            care=feed and any(d>future_day+1 for d in row['productions'])
            service=[Task(point,'HARVEST',animal,generation)]
            if feed:
                service.append(Task(point,'FEED',animal,generation))
            if care:
                service.append(Task(point,'CARE',animal,generation))
            service.append(Task(point,'COLLECT_FERTILIZER',animal,generation))
            tours[row['preferred']].extend(service)
            consecutive[point]=0 if feed else consecutive[point]+1
        completed=[]
        for actor,tasks in tours.items():
            tasks=tuple(tasks)
            products=sorted({rules.ANIMALS[t.item]['product'] for t in tasks if t.operation=='HARVEST'})
            access=nearest_shed(tasks[-1].point)
            tasks+=tuple(Task(access,'DEPOSIT',product,None) for product in products)
            completed.append((actor,tasks))
        for actor,tasks in sorted(completed,key=lambda r:(-_length(r[1]),r[0])):
            _insert_tour(rows,tasks,parent.workers[future_day],actor)
        for actor,sequence in original.items():
            unchanged=tuple(t for t in sequence if t.point not in active and t.operation not in SUPPLY)
            retained=tuple(t for t in rows[actor] if t.point not in active and t.operation not in SUPPLY)
            if unchanged!=retained:
                raise ValueError('Retaining animals reordered another paid site')
        days.append(rows)
    animal=cohort[0]['animal']
    return ProductionProgramme(parent.name+f'-retain-{animal.lower()}-{cadence}-{day}',tuple(days),parent.workers)


def paid_herd_continuations(programme,observation,*,horizon_days=6):
    tick=int(observation['step'])
    if tick%24:
        raise ValueError('Paid herd continuations require a day boundary')
    private=observation['private']
    carried=Counter(private['shed'])
    for bag in private['inventories']:
        carried.update(bag)
    # Unassigned paid replacement animals protect prospective placements.
    if any(carried[a] for a in rules.ANIMALS):
        return ()
    groups=defaultdict(list)
    for row in _planned_exits(programme,observation,horizon_days):
        groups[row['animal']].append(row)
    prices=observation['market']['prices']
    order=sorted(groups,key=lambda animal:(-prices[rules.ANIMALS[animal]['product']],animal))
    return tuple(_retained_programme(programme,groups[animal],observation,cadence)
                 for animal in order for cadence in ('productive','daily'))
