"""Replace unpaid terminal crop cohorts while retaining their labor and harvest slots."""
from dataclasses import replace

from . import rules
from .compiler import distance, nearest_shed
from .programmes import ProductionProgramme
from .task_allocation import Revision


def rotation_variants(programme, observation, controller, *, price):
    tick = int(observation['step'])
    if tick % 24 or not 600 <= tick <= 648:
        return ()
    day = tick//24
    farm = observation['farms'][int(observation['player'])]
    slots = {}
    for d in range(max(26, day), 28):
        for actor, seq in sorted(programme.days[d].items()):
            for task in seq:
                if task.operation != 'PLANT' or task.item not in ('WHEAT', 'MELON') or task.lot or task.generation != d:
                    continue
                tile = farm['tiles'][task.point[1]][task.point[0]]
                if isinstance(tile, dict) and (tile.get('animal') or tile.get('planted_day', -1) >= d):
                    continue
                key = task.point, task.item, task.generation
                service = [(future, t.operation) for future in range(d, 30)
                           for tasks in programme.days[future].values() for t in tasks
                           if (t.point, t.item, t.generation) == key]
                harvest = next((h for h, op in service if op == 'HARVEST' and h >= d+2), None)
                if harvest is None or harvest > d+3:
                    continue
                def units(item):
                    f = dict(tiles=[[rules._new_plant(item, d, 24)]], farmer=[0, 0], hands=[])
                    p = dict(inventories=[{}], seeds={}, shed={})
                    for future in range(d, harvest+1):
                        for h, op in service:
                            if h == future and op in ('WATER', 'FERTILIZE', 'HARVEST'):
                                if op == 'FERTILIZE':
                                    p['inventories'][0]['FERTILIZER'] = 1
                                rules._apply_unit_action(f, p, 0, [op], 1, future, 24, 100)
                        if future < harvest:
                            rules._daily_refresh_plants(f, future, 24)
                    return p['inventories'][0].get(item, 0)
                new_units, old_units = units('CARROT'), units(task.item)
                arrival = min(718, harvest*24+distance(task.point, nearest_shed(task.point))+4)
                gain = price('CARROT', arrival, new_units)-price(task.item, arrival, old_units)-20
                if new_units and gain > 0:
                    slots[key] = dict(point=task.point, item='CARROT', generation=d, end=harvest,
                        lot=f'redeploy:{tick}:{task.point[0]}:{task.point[1]}:CARROT:{d}', estimate=gain)
    ranked = sorted(slots, key=lambda k:(-slots[k]['estimate'], k))
    revisions, seen = [], set()
    for count in (2, 4, 8, 16):
        selected = tuple(ranked[:count])
        if not selected or selected in seen:
            continue
        seen.add(selected)
        changed = set(selected)
        days = []
        for d, actors in enumerate(programme.days):
            rows = {}
            for actor, seq in actors.items():
                tasks = []
                for task in seq:
                    key = task.point, task.item, task.generation
                    if d >= day and key in changed and task.operation in ('PLANT', 'WATER', 'FERTILIZE', 'HARVEST'):
                        task = replace(task, item='CARROT', lot=slots[key]['lot'])
                    tasks.append(task)
                rows[actor] = tuple(tasks)
            days.append(rows)
        p = ProductionProgramme(programme.name+f'-terminal-carrot-{tick}-{len(selected)}', tuple(days), programme.workers)
        if not controller.can_continue(p):
            raise ValueError('A terminal rotation changed an executed prefix')
        lots = [{k:v for k,v in slots[key].items() if k != 'estimate'} for key in selected]
        points = [r['point'] for r in lots]
        revisions.append(Revision(p, dict(kind='rotation_batch', mode='unpaid_terminal_crop_batch', item='CARROT',
            step=tick, point=points[0], points=points, estimate=sum(slots[k]['estimate'] for k in selected),
            marginal_wages=0, retired_animals=[], added_plantings=lots, added_herds=[], lots=lots,
            removed_tasks=[], cancelled_points=[], work=[],
            price_basis='Current continuation harvest slots and unpaid seeds; full native quoting funds the cohort and prices all displaced output')))
    return tuple(revisions)
