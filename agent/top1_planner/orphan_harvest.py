"""Recover visible crop receipts that expire before the next workday."""
from . import rules
from .compiler import distance, nearest_shed
from .programmes import Task


def expiring_crops(farm, day):
    """Candidate sites only; assignments and current tiles are rechecked per actor."""
    if day >= 29:
        return ()
    points = []
    for y, row in enumerate(farm['tiles']):
        for x, tile in enumerate(row):
            if not isinstance(tile, dict) or tile.get('crop') not in rules.CROPS:
                continue
            if not tile.get('yield_units'):
                continue
            crop = rules.CROPS[tile['crop']]
            if day - tile['planted_day'] < crop['first_yield_day']:
                continue
            drought = not tile['watered_today'] and tile['consecutive_unwatered'] >= 1
            expiry = 0 <= tile['max_lifespan_step'] <= (day + 1) * 24
            if drought or expiry:
                points.append((x, y))
    return tuple(points)


def take_orphan_harvest(controller, actor, farm, private, market, tick, points):
    """Append an unclaimed, expiring harvest after all of this actor's work.

    No inputs or additional labor are bought. Existing task reservations take
    precedence. Harvests may use the automatic day-end deposit before day29;
    the normal settlement policy sees the added cargo and sells for capacity.
    """
    if not points or tick // 24 >= 29:
        return None
    day, hour = divmod(tick, 24)
    rows = controller._tasks(day)
    sequence = rows.get(actor, ())
    if controller.cursor.get(actor, 0) < len(sequence):
        return None
    reserved = {task.point for owner, tasks in rows.items()
                for task in tasks[controller.cursor.get(owner, 0):]}
    occupied = sum(private['shed'].values()) + sum(sum(bag.values()) for bag in private['inventories'])
    # Leave current feed and fertilizer reservations with their original jobs.
    liquid = sum(q for item, q in private['shed'].items()
                 if item in rules.PRODUCTS and item not in ('WHEAT', 'FERTILIZER'))
    origin = tuple(rules._farmer_position(farm, actor))
    available = 24 - hour
    candidates = []
    for point in points:
        if point in reserved:
            continue
        tile = farm['tiles'][point[1]][point[0]]
        if not isinstance(tile, dict) or tile.get('crop') not in rules.CROPS or not tile.get('yield_units'):
            continue
        quantity = tile['yield_units']
        excess = max(0, occupied + quantity - controller.cfg['shedCapacity'])
        finish = distance(origin, point) + 1
        if excess > liquid or finish > available:
            continue
        item = tile['crop']
        inventory = market['inventory'][item]
        value = sum(rules.market_price(item, inventory + i, market.get('params')) for i in range(quantity))
        delivery = distance(point, nearest_shed(point)) + 1
        manual = finish + delivery <= available
        duration = finish + delivery if manual else finish
        candidates.append((value / duration, value, -duration, point, item, tile['planted_day'], quantity, excess, manual))
    if not candidates:
        return None
    _, value, _, point, item, born, quantity, excess, manual = max(candidates)
    lot = f'orphan:{day}:{point[0]}:{point[1]}:{item}:{born}'
    tasks = (Task(point, 'HARVEST', item, born, lot),)
    if manual:
        tasks += (Task(nearest_shed(point), 'DEPOSIT', item, None),)
    changed = dict(rows)
    changed[actor] = sequence + tasks
    controller.day_sequences = changed
    if controller.calendar is not None:
        controller.calendar.sequences = dict(changed)
    return dict(kind='orphan_harvest', step=tick, donor=None, recipient=actor, point=point,
                operations=[task.operation for task in tasks], item=item, generation=born,
                quantity=quantity, immediate_sale_value=value, capacity_sale_units=excess,
                delivery='manual' if manual else 'automatic_day_end', extra_hires=0, new_establishment=0)
