"""Complete a delayed near-term herd at its next already scheduled service visit."""
from dataclasses import replace

from top1_planner import rules
from top1_planner.compiler import nearest_shed
from top1_planner.programmes import Task


def continuation(programme, observation, grace=2):
    if grace not in (1,2):
        raise ValueError('A completion window must be one or two service days')
    tick=int(observation['step'])
    if tick%24 or not 144<=tick<=576:
        return None
    today=tick//24
    farm=observation['farms'][int(observation['player'])]
    planned={}
    for day in range(today,min(30,today+3)):
        for sequence in programme.days[day].values():
            for task in sequence:
                if task.operation!='PLACE' or task.item not in rules.ANIMALS or task.lot:
                    continue
                tile=farm['tiles'][task.point[1]][task.point[0]]
                if isinstance(tile,dict) and tile.get('animal'):
                    continue
                planned.setdefault((task.point,task.item,task.generation),day)
    if not planned:
        return None
    days=list(programme.days)
    inserted=[]
    for day in range(today,min(29,today+3+grace)):
        rows=dict(programme.days[day])
        additions={}
        for (point,animal,generation),start in sorted(planned.items()):
            if not start<day<=start+grace:
                continue
            if day+rules.ANIMALS[animal]['first_yield_day']>29:
                continue
            visits=[]
            for actor,sequence in rows.items():
                if any(t.point==point and t.item==animal and t.operation=='PLACE' for t in sequence):
                    visits=[]
                    break
                service=[i for i,t in enumerate(sequence) if t.point==point and t.item==animal
                         and t.generation==generation and t.operation in ('FEED','CARE')]
                if service:
                    first=next(i for i,t in enumerate(sequence) if t.point==point and t.item==animal
                               and t.generation==generation)
                    visits.append((first,actor))
            if not visits:
                continue
            first,actor=min(visits)
            additions.setdefault(actor,[]).append((first,point,animal,generation))
        for actor,jobs in additions.items():
            sequence=list(rows[actor])
            for index,point,animal,generation in sorted(jobs,reverse=True):
                sequence[index:index]=[Task(point,'BUILD_'+rules.ANIMALS[animal]['structure'],None,None),
                                       Task(point,'PLACE',animal,generation)]
                inserted.append(dict(day=day,actor=actor,point=point,animal=animal,generation=generation))
            materials=sorted({job[2] for job in jobs})
            pickups=[Task(nearest_shed(jobs[0][1]),'PICKUP',animal,None) for animal in materials]
            rows[actor]=tuple(pickups+sequence)
        days[day]=rows
    if not inserted:
        return None
    candidate=replace(programme,name=programme.name+f'-complete-herd-{tick}-grace{grace}',days=tuple(days))
    assert candidate.days[:today]==programme.days[:today] and candidate.workers==programme.workers
    return candidate,dict(kind='near_term_herd_completion',step=tick,grace_days=grace,
        visits=inserted,basis='Repeat a planned placement at its next service visit. All land, material, labor, supply and displaced work are charged in the full quote.')


def install(search):
    original=search.unpaid_herd_variants

    def variants(programme,observation,**kwargs):
        ordinary=original(programme,observation,**kwargs)
        extra=[]
        for parent in (*ordinary,programme):
            revised=continuation(parent,observation)
            if revised is not None:
                extra.append(revised[0])
        return (*ordinary,*extra)

    search.unpaid_herd_variants=variants
