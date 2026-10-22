"""Build complete crop-to-herd work bundles, with an explicit construction start and paid crop salvage."""
from top1_planner import rules
from top1_planner.compiler import nearest_shed
from top1_planner.programmes import ProductionProgramme, Task
from top1_planner.redeployment import SITE_WORK
from top1_planner.task_allocation import Revision, _Calendar


def build(controller, observation, groups, *, allow_hire, day_states=(), start_day=None):
    programme = controller.programme
    calendar = _Calendar(programme, observation, controller, day_states)
    day, tick = calendar.day, calendar.start
    first = day if start_day is None else int(start_day)
    if not day <= first <= min(27, day+3):
        raise ValueError('Invalid cohort construction day')
    if calendar.hour > 1:
        raise ValueError('A new cohort requires an early daily funding and construction window')
    points = tuple(tuple(p) for group in groups for p in group)
    if len(set(points)) != len(points) or not points:
        raise ValueError('Cohort sites must be distinct')
    days, workers = calendar.rows(), list(programme.workers)
    lots, removed, work = [], [], []
    tiles = {}
    for p in points:
        tile = calendar.farm['tiles'][p[1]][p[0]]
        if not isinstance(tile, dict) or not tile.get('crop'):
            raise ValueError('This response replaces observed crops only')
        tiles[p] = tile
        for d in range(first,30):
            for actor,sequence in list(days[d].items()):
                prefix=calendar.cursor(d,actor)
                keep=[]
                for index,task in enumerate(sequence):
                    if index>=prefix and task.point==p and task.operation in SITE_WORK:
                        removed.append(dict(day=d,actor=actor,operation=task.operation,item=task.item,generation=task.generation))
                    else:
                        keep.append(task)
                days[d][actor]=tuple(keep)
        lots.append(dict(point=p, item='SHEEP', generation=first, end=29,
            lot=f'redeploy:{tick}:{p[0]}:{p[1]}:SHEEP:{first}'))
    lot_by_point = {tuple(lot['point']): lot['lot'] for lot in lots}
    for d in range(first, 30):
        for group in groups:
            tasks = []
            # Prefetch the whole service tour; the executor nets carried grain
            # and already established animals against these exact obligations.
            if d + rules.ANIMALS['SHEEP']['first_yield_day'] <= 29:
                tasks.append(Task(nearest_shed(group[0]), 'PICKUP', 'SHEEP', None))
            if d < 29:
                tasks.append(Task(nearest_shed(group[0]), 'PICKUP', 'WHEAT', None))
            for p in group:
                p = tuple(p)
                old, lot = tiles[p], lot_by_point[p]
                tasks.append(Task(p, 'CLEAR_CROP', old['crop'], old['planted_day']))
                if d + rules.ANIMALS['SHEEP']['first_yield_day'] <= 29:
                    tasks.append(Task(p, 'PLACE', 'SHEEP', first, lot))
                tasks.append(Task(p, 'HARVEST', 'SHEEP', first, lot))
                if d < 29:
                    tasks.extend((Task(p, 'FEED', 'SHEEP', first, lot),
                        Task(p, 'CARE', 'SHEEP', first, lot)))
                tasks.append(Task(p, 'COLLECT_FERTILIZER', 'SHEEP', first, lot))
            # One shared return for the tour. DROP returns both products and
            # salvaged crops while retaining materials owed to later work.
            tasks.append(Task(nearest_shed(group[-1]), 'DROP', None, None))
            work.append(calendar.insert(days[d], workers, d, tuple(tasks), allow_hire))
    label = '-'.join(str(len(g)) for g in groups)
    sites = '-'.join(f'{p[0]}.{p[1]}' for p in points)
    name = programme.name + f'-cohort-{tick}-start{first}-{sites}-{label}' + ('-hire' if allow_hire else '-paid')
    plan = ProductionProgramme(name, tuple(days), tuple(workers))
    if not controller.can_continue(plan):
        raise ValueError('A cohort changed an already executed task prefix')
    return Revision(plan, dict(kind='productive_redeployment', mode='grouped_crop_herd',
        step=tick, start_day=first, item='SHEEP', points=points, groups=groups, lots=lots, added_herds=lots,
        added_plantings=[], retired_animals=[], removed_tasks=removed, work=work,
        marginal_wages=sum(w['marginal_wage'] for w in work),
        boundary='Candidate construction only; requires a complete physical quote and continuation.'))
