"""Generate unpaid herd alternatives with complete service and delivery tasks."""
from collections import Counter
from dataclasses import replace

from . import rules
from .programmes import ProductionProgramme, Task


def unpaid_herd_variants(programme, observation, *, horizon_days=3):
    """Change prospective cohorts, preserving all paid animals and earlier days.

    An unassigned purchased animal conservatively protects all upcoming cohorts
    of its type. Candidates include the extra harvest, supply and deposit work
    required by their replacement species; the compiler prices their full tail.
    """
    tick = int(observation['step'])
    if tick % 24:
        raise ValueError('Unpaid cohort construction requires a day boundary')
    day = tick // 24
    farm = observation['farms'][int(observation['player'])]
    private = observation['private']
    paid = Counter(private['shed'])
    for bag in private['inventories']:
        paid.update(bag)
    cohort_keys = set()
    for future_day in range(day, min(30, day + horizon_days)):
        for sequence in programme.days[future_day].values():
            for task in sequence:
                if task.operation != 'PLACE' or task.item not in rules.ANIMALS or task.lot:
                    continue
                tile = farm['tiles'][task.point[1]][task.point[0]]
                if paid[task.item] or (isinstance(tile, dict) and tile.get('animal')):
                    continue
                cohort_keys.add((task.point, task.item, task.generation))
    variants = []
    for animal in rules.ANIMALS:
        changed = {key: animal for key in cohort_keys if key[1] != animal}
        if not changed:
            continue
        products = {rules.ANIMALS[key[1]]['product'] for key in changed}
        product = rules.ANIMALS[animal]['product']
        build_changes = {(key[0], key[2]): animal for key in changed}
        days = []
        for future_day, actors in enumerate(programme.days):
            if future_day < day:
                days.append(dict(actors))
                continue
            rows = {}
            for actor, sequence in actors.items():
                rewritten = []
                for task in sequence:
                    key = (task.point, task.item, task.generation)
                    replacement = changed.get(key)
                    if replacement:
                        rewritten.append(replace(task, item=replacement))
                    elif task.operation.startswith('BUILD_') and (task.point, future_day) in build_changes:
                        species = build_changes[(task.point, future_day)]
                        rewritten.append(replace(task, operation='BUILD_' + rules.ANIMALS[species]['structure']))
                    else:
                        rewritten.append(task)
                tasks = []
                for index, (original, task) in enumerate(zip(sequence, rewritten)):
                    if original.operation == 'PICKUP' and original.item in rules.ANIMALS:
                        materials = []
                        for old, following in zip(sequence[index + 1:], rewritten[index + 1:]):
                            if old.operation == 'PICKUP' and old.item == original.item:
                                break
                            if old.operation == 'PLACE' and old.item == original.item and following.item not in materials:
                                materials.append(following.item)
                        tasks.extend(replace(task, item=material) for material in materials)
                        continue
                    changed_asset = (original.point, original.item, original.generation) in changed
                    first_service = task.operation in ('FEED', 'CARE', 'COLLECT_FERTILIZER') and (
                        not tasks or tasks[-1].point != task.point or tasks[-1].operation not in (
                            'HARVEST', 'FEED', 'CARE', 'COLLECT_FERTILIZER'))
                    if changed_asset and first_service:
                        tasks.append(Task(task.point, 'HARVEST', task.item, task.generation))
                    tasks.append(task)
                    if task.operation == 'DEPOSIT' and task.item in products and task.item != product:
                        tasks.append(replace(task, item=product))
                rows[actor] = tuple(tasks)
            days.append(rows)
        candidate = ProductionProgramme(programme.name + '-unpaid-' + animal.lower() + '-' + str(day),
                                        tuple(days), programme.workers)
        variants.append(candidate)
    return tuple(variants)


def unpaid_crop_variants(programme, observation, *, crop='TOMATO', sizes=(2, 4, 8)):
    """Use already-visited wheat plots for a complete alternative crop lifecycle.

    The paid wheat crop is harvested before establishment. During the new crop's
    lifetime, wheat replanting is suspended and the existing daily visits perform
    maintenance and available harvest instead. Feed replacement, seeds, extra
    work, deliveries and market impact are evaluated by the same full compiler.
    """
    tick = int(observation['step'])
    if tick % 24:
        raise ValueError('Crop programme construction requires a day boundary')
    if crop not in rules.CROPS or not rules.CROPS[crop]['ongoing']:
        raise ValueError('This substitution requires an ongoing crop')
    day = tick // 24
    farm = observation['farms'][int(observation['player'])]
    data = rules.CROPS[crop]
    duration = data['first_yield_day'] + (data['max_yield'] - 1) * data['interval'] + 1
    starts = {}
    for future_day in range(day, min(30, day + 3)):
        for sequence in programme.days[future_day].values():
            for task in sequence:
                if task.operation != 'PLANT' or task.item != 'WHEAT' or task.point in starts or task.lot:
                    continue
                tile = farm['tiles'][task.point[1]][task.point[0]]
                if isinstance(tile, dict) and (tile.get('animal') or (tile.get('crop') and tile['crop'] != 'WHEAT')):
                    continue
                end = future_day + duration
                if end > 29:
                    continue
                compatible = True
                for check_day in range(future_day, end):
                    visits = [other for seq in programme.days[check_day].values() for other in seq if other.point == task.point]
                    if any(other.operation == 'PLACE' or (other.operation == 'PLANT' and other.item != 'WHEAT') for other in visits):
                        compatible = False
                if compatible:
                    starts[task.point] = future_day
    if not starts:
        return ()
    ordered = sorted(starts, key=lambda point: (starts[point],
        min(abs(point[0]-x)+abs(point[1]-y) for x,y in ((4,4),(5,4),(4,5),(5,5))),point))
    results = []
    seen_sets = set()
    for count in sizes:
        selected = tuple(ordered[:count])
        if not selected or selected in seen_sets:
            continue
        seen_sets.add(selected)
        days = []
        for future_day, actors in enumerate(programme.days):
            rows = {}
            for actor, sequence in actors.items():
                tasks = []
                for task in sequence:
                    active = task.point in selected and starts[task.point] <= future_day < starts[task.point] + duration
                    if active and future_day == starts[task.point] and task.generation is not None and task.generation < starts[task.point]:
                        active = False
                    if not active or task.operation in ('PICKUP','DROP','DEPOSIT'):
                        tasks.append(task)
                        continue
                    first_plant = task.operation == 'PLANT' and future_day == starts[task.point]
                    if first_plant:
                        tasks.append(replace(task,item=crop,generation=starts[task.point]))
                        continue
                    if task.operation == 'DIG':
                        continue
                    if task.operation in ('PLANT','WATER','HARVEST','FERTILIZE'):
                        begin_visit = not tasks or tasks[-1].point != task.point or tasks[-1].item != crop
                        if begin_visit:
                            tasks.append(Task(task.point,'HARVEST',crop,starts[task.point]))
                            tasks.append(Task(task.point,'WATER',crop,starts[task.point]))
                        if task.operation == 'FERTILIZE':
                            age = future_day - starts[task.point]
                            productive = any(age+offset+1 >= data['first_yield_day']
                                and (age+offset+1-data['first_yield_day']) % data['interval'] == 0
                                and (age+offset+1-data['first_yield_day']) // data['interval'] < data['max_yield']
                                for offset in range(3))
                            if productive:
                                tasks.append(replace(task,item=crop,generation=starts[task.point]))
                        elif not begin_visit and task.operation != 'PLANT':
                            tasks.append(replace(task,item=crop,generation=starts[task.point]))
                        continue
                    tasks.append(task)
                rows[actor] = tuple(tasks)
            # Wheat may omit a safe maintenance day; the replacement crop gets
            # an explicit visit assigned inside the existing labor schedule.
            def walk_length(a,b):
                return abs(a[0]-b[0])+abs(a[1]-b[1])
            for point in selected:
                if not starts[point] <= future_day < starts[point]+duration:
                    continue
                if any(task.point==point and task.item==crop and task.operation in ('WATER','PLANT')
                       for sequence in rows.values() for task in sequence):
                    continue
                choices=[]
                for actor in range(programme.workers[future_day]+1):
                    sequence=rows.get(actor,())
                    route=[(4,4),*(task.point for task in sequence)]
                    load=len(sequence)+sum(walk_length(a,b) for a,b in zip(route,route[1:]))
                    for index in range(len(sequence)+1):
                        before=(4,4) if index==0 else sequence[index-1].point
                        after=sequence[index].point if index<len(sequence) else None
                        if after is not None and after==before:
                            continue
                        extra=walk_length(before,point)+2
                        if after is not None:
                            extra+=walk_length(point,after)-walk_length(before,after)
                        choices.append((max(0,load+extra-24),extra,load,actor,index))
                _,_,_,actor,index=min(choices)
                sequence=rows.get(actor,())
                visit=(Task(point,'HARVEST',crop,starts[point]),Task(point,'WATER',crop,starts[point]))
                rows[actor]=sequence[:index]+visit+sequence[index:]
            days.append(rows)
        results.append(ProductionProgramme(programme.name+'-unpaid-'+crop.lower()+'-'+str(day)+'-'+str(len(selected)),
                                           tuple(days),programme.workers))
    return tuple(results)
