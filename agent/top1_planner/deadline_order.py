"""Yield optional inserted-lot watering to mandatory crop visits in the same queue."""
from . import rules
from .compiler import distance
from .redeployment import identity


def _harvest_instead(tile, day, tick):
    crop = rules.CROPS[tile['crop']]
    ready = not crop['ongoing'] and tile['yield_units'] > 0 and day - tile['planted_day'] >= crop['first_yield_day']
    complete = tile['yield_units'] >= crop['max_yield'] or day - tile['planted_day'] > crop['max_yield_day']
    last = (tick + 1) % 24 == 0 and tick + 1 >= tile['max_lifespan_step'] >= 0
    return ready and (complete or last)


def _water_times(controller, sequence, position, farm, day, tick):
    """Estimate optimistic travel and crop work without waits or supply stops.

    This screens for deadline risk, not complete feasibility or economic
    value. The native continuation remains responsible for those.
    """
    tiles = {}
    elapsed = 0
    visits = {}
    for task in sequence:
        point, op, item = task.point, task.operation, task.item
        if point not in tiles:
            original = farm['tiles'][point[1]][point[0]]
            tiles[point] = dict(original) if isinstance(original, dict) else original
        tile = tiles[point]
        plant = isinstance(tile, dict) and tile.get('crop') in rules.CROPS
        animal = isinstance(tile, dict) and bool(tile.get('animal'))
        if op in ('DROP', 'DEPOSIT', 'PICKUP'):
            continue
        if task.lot and op in ('WATER', 'FERTILIZE', 'HARVEST') and identity(task,tile) is None:
            continue
        if op == 'PLANT':
            if animal or (plant and tile['crop'] == item and tile['planted_day'] >= task.generation):
                continue
            if plant and controller.adaptive_crops:
                crop = rules.CROPS[tile['crop']]
                age = day - tile['planted_day']
                ready = tile['yield_units'] and age >= crop['first_yield_day']
                cleared = crop['ongoing'] and age >= crop['first_yield_day'] + (crop['max_yield'] - 1) * crop['interval'] and not tile['yield_units']
                if not ready and not cleared:
                    continue
            tiles[point] = rules._new_plant(item, day, 24)
        elif op in ('DIG', 'CLEAR_CROP'):
            if tile is None or animal:
                continue
            if op == 'CLEAR_CROP' and not (plant and tile['crop'] == item and tile['planted_day'] == task.generation):
                continue
            tiles[point] = None
        elif op == 'WATER':
            if not plant or tile['watered_today'] or (tile['crop'] != item and not controller.adaptive_crops):
                continue
            if _harvest_instead(tile, day, tick + elapsed + distance(position, point)):
                tiles[point] = None
            else:
                if tile['consecutive_unwatered'] >= 1:
                    key = (point, tile['crop'], tile['planted_day'])
                    visits[key] = elapsed + distance(position, point) + 1
                tile['watered_today'] = True
        elif op == 'HARVEST':
            if not (plant or animal) or not tile.get('yield_units'):
                continue
            if plant and day - tile['planted_day'] < rules.CROPS[tile['crop']]['first_yield_day']:
                continue
            if plant and not rules.CROPS[tile['crop']]['ongoing']:
                tiles[point] = None
            else:
                tile['yield_units'] = 0
        elif op == 'FERTILIZE':
            if not plant or tile.get('fertilized_until_day', -1) >= day + 2:
                continue
            # Omitting optional fertilizer work keeps this travel estimate optimistic.
            continue
        elif op in ('FEED', 'CARE', 'COLLECT_FERTILIZER'):
            if not animal:
                continue
            flag = {'FEED': 'fed_today', 'CARE': 'cared_today', 'COLLECT_FERTILIZER': 'fertilizer_available'}[op]
            needed = tile.get(flag, False) if op == 'COLLECT_FERTILIZER' else not tile.get(flag, False)
            if not needed:
                continue
            tile[flag] = op != 'COLLECT_FERTILIZER'
        else:
            # Establishment dependencies outside crop work need a full native quote.
            return None
        elapsed += distance(position, point) + 1
        position = point
    return visits


def defer_optional_water(controller, actor, farm, day, tick):
    rows = controller._tasks(day)
    sequence = rows.get(actor, ())
    cursor = controller.cursor.get(actor, 0)
    if cursor >= len(sequence):
        return None
    task = sequence[cursor]
    # Inserted lots may yield their optional service; inherited tours retain
    # their established crop and material ordering.
    if task.operation != 'WATER' or not task.lot:
        return None
    tile = farm['tiles'][task.point[1]][task.point[0]]
    if not (isinstance(tile, dict) and tile.get('crop') in rules.CROPS):
        return None
    if tile['watered_today'] or tile['consecutive_unwatered'] >= 1:
        return None
    if task.lot and not (tile['crop'] == task.item and tile['planted_day'] >= task.generation):
        return None
    if tile['crop'] != task.item and not controller.adaptive_crops:
        return None
    if _harvest_instead(tile, day, tick) or 0 <= tile['max_lifespan_step'] <= (day + 1) * 24:
        return None
    # Keep same-site harvest, fertilizer, establishment and material dependencies together.
    if any(other.point == task.point for owner, tasks in rows.items()
           for index, other in enumerate(tasks)
           if index >= controller.cursor.get(owner, 0) and (owner != actor or index != cursor)):
        return None
    available = min(24 - tick % 24, 719 - tick)
    if available <= 1:
        return None
    pos = tuple(rules._farmer_position(farm, actor))
    before = _water_times(controller, sequence[cursor:], pos, farm, day, tick)
    if before is None:
        return None
    missed = {key for key, eta in before.items() if eta > available}
    if not missed:
        return None
    suffix = sequence[cursor + 1:] + (task,)
    after = _water_times(controller, suffix, pos, farm, day, tick)
    if after is None or before.keys() != after.keys():
        return None
    new_missed = {key for key, eta in after.items() if eta > available}
    if not new_missed < missed:
        return None
    changed = dict(rows)
    changed[actor] = sequence[:cursor] + suffix
    controller.day_sequences = changed
    if controller.calendar is not None:
        controller.calendar.sequences = dict(changed)
    return dict(step=tick, actor=actor, deferred_point=task.point, item=tile['crop'], lot=task.lot,
        available_steps=available,
        recovered=[dict(point=key[0], item=key[1], generation=key[2],
                        earlier_eta=before[key], revised_eta=after[key]) for key in sorted(missed - new_missed)],
        scope='Optimistic own-queue deadline ordering; no new inputs, ownership transfer or profit guarantee')
