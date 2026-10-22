"""Replace a paid berry crop by repeated short crops on its inherited visits."""
from top1_planner import rules
from top1_planner.compiler import nearest_shed
from top1_planner.programmes import ProductionProgramme, Task
from top1_planner.redeployment import SITE_WORK, crop_cycles
from top1_planner.task_allocation import Revision, _Calendar


def eligible_points(controller, observation):
    day = int(observation['step'])//24
    farm = observation['farms'][int(observation['player'])]
    result = []
    for y, row in enumerate(farm['tiles']):
        for x, tile in enumerate(row):
            if not isinstance(tile, dict) or tile.get('crop') != 'STRAWBERRY':
                continue
            if day-tile['planted_day'] < rules.CROPS['STRAWBERRY']['first_yield_day']:
                continue
            point = x, y
            visits = [(d, a, t) for d in range(day, 30) for a, seq in controller.programme.days[d].items()
                for t in seq if t.point == point and t.operation in SITE_WORK]
            if not visits or any(t.lot or t.operation in ('PLACE', 'BUILD_COOP', 'BUILD_PASTURE')
                                 for d, a, t in visits):
                continue
            visit_days = len({d for d, a, t in visits})
            result.append(dict(point=point, born=tile['planted_day'], visit_days=visit_days,
                old_yield=tile['yield_units']))
    return tuple(sorted(result, key=lambda r:(r['born'], -r['visit_days'], r['point'])))


def build(controller, observation, points, *, period=2, first=None, day_states=()):
    tick = int(observation['step'])
    if tick % 24 and first not in (tick//24+1,tick//24+2):
        raise ValueError('Intraday crop turnover starts on an adjacent unpaid day')
    day = tick//24
    first = day if first is None else first
    if first not in (day,day+1,day+2):
        raise ValueError('The first cycle must start within the next two days')
    points = tuple(tuple(p) for p in points)
    allowed = {r['point'] for r in eligible_points(controller, observation)}
    if not points or len(set(points)) != len(points) or not set(points) <= allowed:
        raise ValueError('Crop turnover requires distinct mature, unclaimed berry sites')
    calendar = _Calendar(controller.programme, observation, controller, day_states)
    original = calendar.rows()
    rows = [dict(d) for d in original]
    workers = list(controller.programme.workers)
    lots, blocks, removed, inserted, inherited = [], {}, [], [], []
    for point in points:
        old = calendar.farm['tiles'][point[1]][point[0]]
        cycles = crop_cycles('CARROT', first, point, period=period)
        for born, end in cycles:
            lot = f'redeploy:route:{tick}:{point[0]}:{point[1]}:CARROT:{born}'
            lots.append(dict(point=point, item='CARROT', generation=born, end=end, lot=lot))
        point_lots = [r for r in lots if r['point'] == point]
        for d in range(first, 30):
            block = [Task(point, 'CLEAR_CROP', 'STRAWBERRY', old['planted_day'])]
            for index, spec in enumerate(point_lots):
                born, end, lot = spec['generation'], spec['end'], spec['lot']
                following = point_lots[index+1]['generation'] if index+1 < len(point_lots) else 30
                if not born <= d <= min(29, following):
                    continue
                if d + rules.CROPS['CARROT']['first_yield_day'] <= 29:
                    block.append(Task(point, 'PLANT', 'CARROT', born, lot))
                block.append(Task(point, 'WATER', 'CARROT', born, lot))
                if d >= end:
                    block.append(Task(point, 'HARVEST', 'CARROT', born, lot))
            blocks[d, point] = tuple(block)
    for d in range(first, 30):
        # Keep the first existing visit as the production handoff; other work
        # on the same crop is replaced as one declared plot-level obligation.
        anchors = {}
        for actor, seq in sorted(original[d].items()):
            for index, task in enumerate(seq):
                if task.point in points and task.operation in SITE_WORK:
                    if index < calendar.cursor(d, actor):
                        raise ValueError('A crop route would change an executed prefix')
                    anchors.setdefault(task.point, (actor, index))
        touched = set()
        for actor, seq in original[d].items():
            result = []
            for index, task in enumerate(seq):
                if task.point in points and task.operation in SITE_WORK:
                    removed.append(dict(day=d, actor=actor, operation=task.operation,
                        point=task.point, item=task.item, generation=task.generation))
                    if anchors[task.point] == (actor, index):
                        result.extend(blocks[d, task.point])
                        inherited.append(dict(day=d, actor=actor, point=task.point, index=index))
                        touched.add(actor)
                else:
                    result.append(task)
            rows[d][actor] = tuple(result)
        for point in points:
            if point not in anchors:
                event = calendar.insert(rows[d], workers, d, blocks[d, point], False)
                inserted.append(event)
                touched.add(event['actor'])
        for actor in sorted(touched):
            seq = rows[d][actor]
            last_work = max((i for i,t in enumerate(seq)
                if t.point in points and t.operation in SITE_WORK), default=-1)
            if last_work < 0:
                raise ValueError('An inherited service actor lost its crop tasks')
            if not any(t.operation == 'DROP' or t.operation == 'DEPOSIT' and t.item == 'CARROT'
                       for t in seq[last_work+1:]):
                # Use the final planned return where possible. A new return
                # remains a charged physical action in the complete quote.
                end = seq[-1].point if seq else calendar.origin(d, actor)
                rows[d][actor] = seq+(Task(nearest_shed(end), 'DEPOSIT', 'CARROT', None),)
    suffix = '-berry-route-'+str(tick)+'-'+str(first)+'-'+str(period)+'-'
    suffix += '-'.join(f'{p[0]}.{p[1]}' for p in points)
    plan = ProductionProgramme(controller.programme.name+suffix, tuple(rows), tuple(workers))
    if not controller.can_continue(plan):
        raise ValueError('The inherited crop route changed an executed prefix')
    assert plan.workers == controller.programme.workers
    return Revision(plan, dict(kind='productive_redeployment', mode='inherited_berry_turnover',
        item='CARROT', point=points[0], points=points, step=tick, first=first, period=period,
        lots=lots, added_plantings=lots, added_herds=[], retired_animals=[], marginal_wages=0,
        removed_tasks=removed, work=inserted, inherited_visits=inherited,
        boundary='Observed mature crop replacement; existing workers and paid animals retained. Every crop cycle, return and displaced future crop requires complete native admission.'))


def variants(programme, observation, controller, *, price, day_states=(), first=None):
    """Offer bounded regional turnover bundles before complete physical pricing."""
    tick = int(observation['step'])
    if not 360 <= tick <= 578 or not controller.can_continue(programme):
        return ()
    from top1_planner.crop_input_value import crop_input_value
    from top1_planner.task_allocation import _site_loss
    first = tick//24 if first is None else first
    owner = controller.fork(programme=programme, settlement=True)
    eligible = eligible_points(owner, observation)
    rows = []
    for region in ('NW', 'NE', 'SW', 'SE'):
        pool = [r for r in eligible if ('N' if r['point'][1] < 5 else 'S')
                +('W' if r['point'][0] < 5 else 'E') == region]
        seen = set()
        for count in (2, 4):
            points = tuple(r['point'] for r in pool[:count])
            if not points or points in seen:
                continue
            seen.add(points)
            estimate = sum(crop_input_value(p, 'CARROT', crop_cycles('CARROT', first, p, period=3),
                price, False)-_site_loss(programme, p, tick, 30, observation, price) for p in points)
            if estimate <= 0:
                continue
            revision = build(owner, observation, points, period=3, first=first, day_states=day_states)
            revision.detail.update(estimate=estimate, region=region,
                price_basis='Inherited biological cash difference after displaced crop cycles and seeds; labor, capacity and bilateral market settlement are decided by full native quotation')
            rows.append(revision)
    rows.sort(key=lambda r:(-r.detail['estimate'], r.programme.name))
    return tuple(rows[:4])
