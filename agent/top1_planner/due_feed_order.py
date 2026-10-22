"""Protect a funded feeding obligation in an animal's last survival slot."""
from . import rules
from .service_cycle import service_required


def prioritize_due_feed(controller, actor, farm, private, day, tick):
    if day >= 29 or tick % 24 != 23:
        return None
    if private['inventories'][actor].get('WHEAT', 0) < 1:
        return None
    rows = controller._tasks(day)
    sequence = rows.get(actor, ())
    start = controller.cursor.get(actor, 0)
    if start >= len(sequence):
        return None
    current = sequence[start]
    if current.operation not in ('HARVEST', 'CARE', 'COLLECT_FERTILIZER'):
        return None
    if tuple(rules._farmer_position(farm, actor)) != current.point:
        return None
    tile = farm['tiles'][current.point[1]][current.point[0]]
    if not (isinstance(tile, dict) and tile.get('animal') == current.item
            and tile.get('placed_day') == current.generation
            and not tile['fed_today'] and tile['consecutive_unfed'] >= 1):
        return None
    for index in range(start + 1, len(sequence)):
        task = sequence[index]
        if (task.point != current.point or task.item != current.item
                or task.generation != current.generation or task.lot != current.lot
                or task.operation not in ('HARVEST', 'FEED', 'CARE', 'COLLECT_FERTILIZER')):
            return None
        if task.operation != 'FEED':
            continue
        if not service_required(task, tile, day):
            return None
        changed = dict(rows)
        changed[actor] = sequence[:start] + (task,) + sequence[start:index] + sequence[index + 1:]
        controller.day_sequences = changed
        if controller.calendar is not None:
            controller.calendar.sequences = dict(changed)
        return dict(kind='due_feed_order', step=tick, actor=actor, point=task.point,
                    item=task.item, generation=task.generation, advanced='FEED',
                    deferred=current.operation, carried_wheat=private['inventories'][actor]['WHEAT'],
                    added_tasks=0, extra_hires=0, new_material_purchases=0)
    return None
