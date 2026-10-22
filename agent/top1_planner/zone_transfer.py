"""Compile a new regional programme without replacing paid-region work."""
from collections import Counter
from . import rules
from .compiler import distance, nearest_shed
from .programmes import ProductionProgramme, Task


_SUPPLY={'PICKUP','DROP','DEPOSIT'}


def _regional(task):
    return task.operation not in _SUPPLY and task.point[0]<5 and task.point[1]>=5


def _length(sequence):
    route=((4,4),*(task.point for task in sequence))
    return len(sequence)+sum(distance(a,b) for a,b in zip(route,route[1:]))


def _insert_tour(rows,tour,workers,preferred_actor):
    """Keep paid-site order while placing a coherent new service tour."""
    choices=[]
    for actor in range(workers+1):
        sequence=rows.get(actor,())
        old_length=_length(sequence)
        for index in range(len(sequence)+1):
            if 0<index<len(sequence) and sequence[index-1].point==sequence[index].point:
                continue
            merged=sequence[:index]+tour+sequence[index:]
            length=_length(merged)
            choices.append((max(0,length-24),length-old_length,length,actor!=preferred_actor,actor,index))
    *_,actor,index=min(choices)
    sequence=rows.get(actor,())
    rows[actor]=sequence[:index]+tour+sequence[index:]


def transfer_unpaid_sw(parent,donor,observation):
    """Replace only the still-locked SW region, retaining outside tasks in order.

    Supply quantities, buying, construction dependencies and all unit actions
    remain the physical compiler's responsibility. Added tours use the parent's
    existing worker calendar; a complete quote measures missed work and costs.
    """
    tick=int(observation['step'])
    if tick!=216:
        return None
    farm=observation['farms'][int(observation['player'])]
    if any(farm['tiles'][y][x]!='LOCKED' for y in range(5,10) for x in range(5)):
        return None
    private=observation['private']
    paid=Counter(private['shed'])
    for bag in private['inventories']:
        paid.update(bag)
    if any(paid[item] for item in rules.ANIMALS):
        return None
    if parent.days[9:]==donor.days[9:]:
        return None
    days=list(parent.days[:9])
    for day in range(9,30):
        original=parent.days[day]
        rows={actor:tuple(task for task in sequence if not _regional(task))
              for actor,sequence in original.items()}
        tours=[]
        for actor,sequence in donor.days[day].items():
            tour=tuple(task for task in sequence if _regional(task))
            if not tour:
                continue
            delivered=[]
            for task in tour:
                if task.operation=='HARVEST' and task.item in rules.ANIMALS:
                    product=rules.ANIMALS[task.item]['product']
                    if product in ('MILK','WOOL') and product not in delivered:
                        delivered.append(product)
            access=nearest_shed(tour[-1].point)
            tour+=tuple(Task(access,'DEPOSIT',item,None) for item in delivered)
            tours.append((actor,tour))
        for actor,tour in sorted(tours,key=lambda row:(-_length(row[1]),row[0])):
            _insert_tour(rows,tour,parent.workers[day],actor)
        for actor,sequence in original.items():
            old_sites=tuple(t for t in sequence if t.operation not in _SUPPLY and not _regional(t))
            new_sites=tuple(t for t in rows[actor] if t.operation not in _SUPPLY and not _regional(t))
            if old_sites!=new_sites:
                raise ValueError('Regional transfer altered paid-region work')
        days.append(rows)
    return ProductionProgramme(parent.name+'-unpaid-sw-'+donor.name.rsplit('-',1)[-1],tuple(days),parent.workers)


def _signature(programme):
    return tuple((day,actor,tuple(task for task in sequence if _regional(task)))
        for day in range(9,30) for actor,sequence in sorted(programme.days[day].items()))


def _products(programme):
    animals={task.item for day in programme.days[9:12] for sequence in day.values()
             for task in sequence if _regional(task) and task.operation=='PLACE'}
    return tuple(sorted(rules.ANIMALS[item]['product'] for item in animals))


def second_wave_variants(parent,observation,library,contexts,limit=2):
    """Propose nearby observed regional plans, then require full-policy admission.

    Current public shop demand and paid herd counts determine proposal order.
    Similarity is a descriptive prior; it is never used as a profit estimate.
    """
    if int(observation['step'])!=216:
        return ()
    farm=observation['farms'][int(observation['player'])]
    herd=Counter(tile['animal'] for row in farm['tiles'] for tile in row
                 if isinstance(tile,dict) and tile.get('animal'))
    demand=Counter()
    for shop in observation['town']['unlocked_shops']:
        demand.update(rules.SHOPS[shop])
    prices=observation['market']['prices']
    def proximity(programme):
        context=contexts[programme.name]
        return (sum(abs(demand[item]-context['demand'].get(item,0)) for item in ('MILK','WOOL')),
                sum(abs(herd[item]-context['paid_animals'].get(item,0)) for item in rules.ANIMALS),
                sum(abs(prices[item]-context['prices'][item])/rules.MARKET_PARAMS[item]['base'] for item in ('MILK','WOOL','EGG')),
                programme.name)
    ranked=sorted(library,key=proximity)
    # Reserve proposal coverage for distinct product mixes before considering
    # another recipe of the same mix. Similarity cannot price new investment.
    groups={}
    for donor in ranked:
        groups.setdefault(_products(donor),[]).append(donor)
    diverse=[]
    for offset in range(max(map(len,groups.values()))):
        diverse.extend(group[offset] for group in groups.values() if offset<len(group))
    seen={_signature(parent)}
    variants=[]
    for donor in diverse:
        signature=_signature(donor)
        if signature in seen:
            continue
        seen.add(signature)
        candidate=transfer_unpaid_sw(parent,donor,observation)
        if candidate is not None:
            variants.append(candidate)
        if len(variants)==limit:
            break
    return tuple(variants)
