"""Restore missing survival work for a crop retained by the paid continuation."""
from . import rules
from .compiler import distance
from .programmes import Task
from .redeployment import managed


_REPLACEMENT = {'DIG', 'CLEAR_CROP', 'PLANT', 'PLACE', 'BUILD_COOP', 'BUILD_PASTURE'}


def _same_crop(controller, task, point, tile):
    if task.point != point or task.item != tile['crop']:
        return False
    if managed(task):
        return controller.lot_bindings.get(task.lot) == (point, tile['crop'], tile['planted_day'])
    return task.generation == tile['planted_day']


def _future_index(controller, day):
    cached = getattr(controller, '_survival_future_index', None)
    if cached is not None and cached[0] is controller.programme and cached[1] == day:
        return cached[2]
    sites = {}
    for future_day in range(day + 1, 30):
        for actor, sequence in controller.programme.days[future_day].items():
            for index, task in enumerate(sequence):
                if task.operation in _REPLACEMENT or task.operation in ('WATER', 'HARVEST'):
                    sites.setdefault(task.point, {}).setdefault(future_day, []).append((actor, index, task))
    controller._survival_future_index = (controller.programme, day, sites)
    return sites


def _retained_harvest(controller, day, point, tile, visits):
    last_water = day
    maturity = tile['planted_day'] + rules.CROPS[tile['crop']]['first_yield_day']
    expiry = tile['max_lifespan_step']
    for future_day, rows in visits.items():
        if future_day - last_water > 2:
            return None
        changes = [(actor, index, task) for actor, index, task in rows if task.operation in _REPLACEMENT]
        matching = [(actor, index, task) for actor, index, task in rows
                    if task.operation in ('WATER', 'HARVEST') and _same_crop(controller, task, point, tile)]
        for actor, index, task in matching:
            if task.operation != 'HARVEST' or future_day < maturity:
                continue
            if expiry >= 0 and expiry <= future_day * 24:
                return None
            if any(owner != actor or cursor < index for owner, cursor, _ in changes):
                continue
            return dict(day=future_day, actor=actor, task=task)
        if changes:
            return None
        if any(task.operation == 'WATER' for _, _, task in matching):
            last_water = future_day
        elif future_day - last_water >= 2:
            return None
    return None


def take_survival_water(controller, actor, farm, private, market, tick):
    """Append one missing watering without moving an existing task or buying labor."""
    day, hour = divmod(tick, 24)
    if day >= 29:
        return None
    if controller.survival_admission is None and day not in controller.survival_approved.values():
        return None
    rows = controller._tasks(day)
    sequence = rows.get(actor, ())
    if controller.cursor.get(actor, 0) < len(sequence):
        return None
    origin = tuple(rules._farmer_position(farm, actor))
    available = 24 - hour
    reserved = {task.point for owner, tasks in rows.items()
                for task in tasks[controller.cursor.get(owner, 0):]}
    candidates = []
    index = None
    for y, row in enumerate(farm['tiles']):
        for x, tile in enumerate(row):
            point = (x, y)
            if (point in reserved or not isinstance(tile, dict) or tile.get('crop') not in rules.CROPS
                    or tile['watered_today'] or tile['consecutive_unwatered'] < 1):
                continue
            key = (point, tile['crop'], tile['planted_day'])
            if controller.survival_denied.get(key) == day:
                continue
            steps = distance(origin, point) + 1
            if steps > available:
                continue
            if index is None:
                index = _future_index(controller, day)
            retained = _retained_harvest(controller, day, point, tile, index.get(point, {}))
            if retained is None:
                continue
            value = market['prices'][tile['crop']] * rules.CROPS[tile['crop']]['max_yield']
            candidates.append((value / steps, value, -steps, point, tile, retained))
    if not candidates:
        return None
    _, _, _, point, tile, retained = max(candidates, key=lambda row: row[:4])
    key = (point, tile['crop'], tile['planted_day'])
    proposal = dict(point=point, item=tile['crop'], generation=tile['planted_day'])
    if controller.survival_approved.get(key) != day:
        if controller.survival_admission is None:
            return None
        if not controller.survival_admission(proposal):
            controller.survival_denied[key] = day
            return None
        controller.survival_approved[key] = day
    witness = retained['task']
    task = Task(point, 'WATER', witness.item, witness.generation, witness.lot)
    changed = dict(rows)
    changed[actor] = sequence + (task,)
    controller.day_sequences = changed
    if controller.calendar is not None:
        controller.calendar.sequences = dict(changed)
    return dict(kind='survival_water', step=tick, recipient=actor, point=point,
                item=tile['crop'], generation=tile['planted_day'], operations=['WATER'],
                deadline=(day + 1) * 24 - 1, retained_harvest_day=retained['day'],
                retained_harvest_actor=retained['actor'], extra_hires=0, material_cost=0,
                new_establishment=0, paid_prefix_changed=False)
