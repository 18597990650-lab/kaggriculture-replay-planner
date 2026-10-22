"""Keep an unstarted crop crew's first watering inside its actual workday."""
from . import rules
from .compiler import distance


def fit_first_watering(controller, actor, farm, private, tick):
    """Reorder independent planting visits only when their present tour overruns.

    Each visit retains its seed and same-site first watering. Paid plots,
    material collection, service tours and already executed tasks are excluded.
    The small subset dynamic programme minimizes travel; successful execution
    still depends on actual seed procurement and land availability.
    """
    day, hour = divmod(tick, 24)
    rows = controller._tasks(day)
    sequence = rows.get(actor, ())
    if controller.cursor.get(actor, 0) or private['inventories'][actor]:
        return None
    if not 4 <= len(sequence) <= 16 or len(sequence) % 2:
        return None
    visits, points, work = [], set(), 0
    for index in range(0, len(sequence), 2):
        plant = sequence[index]
        water = sequence[index + 1]
        if (plant.operation != 'PLANT' or water.operation != 'WATER'
                or plant.item not in rules.CROPS or plant.generation != day
                or (plant.point, plant.item, plant.generation, plant.lot)
                != (water.point, water.item, water.generation, water.lot)
                or plant.point in points):
            return None
        tile = farm['tiles'][plant.point[1]][plant.point[0]]
        if tile is not None and tile != 'LOCKED' and not (
                isinstance(tile, dict) and tile.get('kind') == 'WEED'):
            return None
        points.add(plant.point)
        visits.append((plant, water))
        work += 2 + int(isinstance(tile, dict) and tile.get('kind') == 'WEED')
    origin = tuple(rules._farmer_position(farm, actor))
    positions = [pair[0].point for pair in visits]
    original = distance(origin, positions[0]) + sum(
        distance(a, b) for a, b in zip(positions, positions[1:]))
    available = min(24 - hour, 719 - tick)
    if original + work <= available:
        return None
    count = len(visits)
    paths = {(1 << index, index): (distance(origin, point), (index,))
             for index, point in enumerate(positions)}
    for mask in range(1, 1 << count):
        for last in range(count):
            prefix = paths.get((mask, last))
            if prefix is None:
                continue
            cost, order = prefix
            for nxt in range(count):
                if mask & (1 << nxt):
                    continue
                key = (mask | (1 << nxt), nxt)
                value = (cost + distance(positions[last], positions[nxt]), order + (nxt,))
                if key not in paths or value < paths[key]:
                    paths[key] = value
    best, order = min(paths[((1 << count) - 1, last)] for last in range(count))
    if best + work > available or best >= original:
        return None
    changed = dict(rows)
    changed[actor] = tuple(task for index in order for task in visits[index])
    controller.day_sequences = changed
    if controller.calendar is not None:
        controller.calendar.sequences = dict(changed)
    return dict(step=tick, actor=actor, origin=origin,
                before=[list(point) for point in positions],
                after=[list(positions[index]) for index in order],
                previous_steps=original + work, revised_steps=best + work,
                available_steps=available, extra_hires=0, extra_inputs=0,
                scope='Unstarted planting and first-water pairs; no paid-prefix change or execution guarantee')
