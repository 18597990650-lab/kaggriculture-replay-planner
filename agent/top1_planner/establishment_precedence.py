"""Order an actor's establishment before service of the same unpaid asset."""
from . import rules


def ensure_establishment_precedence(controller, actor, farm, day, tick):
    rows = controller._tasks(day)
    sequence = rows.get(actor, ())
    start = controller.cursor.get(actor, 0)
    if start >= len(sequence):
        return None
    service = sequence[start]
    if service.operation not in ('FEED', 'CARE', 'WATER', 'FERTILIZE'):
        return None
    point = service.point
    tile = farm['tiles'][point[1]][point[0]]
    if tile is not None and (not isinstance(tile, dict) or tile.get('crop') or tile.get('animal')):
        return None
    expected = 'PLACE' if service.item in rules.ANIMALS else 'PLANT'
    end_service = start
    while end_service < len(sequence):
        task = sequence[end_service]
        if (task.point != point or task.operation not in ('FEED', 'CARE', 'WATER', 'FERTILIZE', 'HARVEST', 'COLLECT_FERTILIZER')
                or task.item != service.item or task.generation != service.generation or task.lot != service.lot):
            break
        end_service += 1
    for end in range(end_service, len(sequence)):
        task = sequence[end]
        if task.point != point or task.operation not in ('DIG', 'BUILD_COOP', 'BUILD_PASTURE', 'PLANT', 'PLACE'):
            return None
        if task.operation != expected:
            continue
        if task.item != service.item or task.generation != service.generation or task.lot != service.lot:
            return None
        establishment = sequence[end_service:end + 1]
        dependent = sequence[start:end_service]
        if not dependent:
            return None
        changed = dict(rows)
        changed[actor] = sequence[:start] + establishment + dependent + sequence[end + 1:]
        controller.day_sequences = changed
        if controller.calendar is not None:
            controller.calendar.sequences = dict(changed)
        return dict(kind='establishment_precedence', step=tick, actor=actor, point=point,
                    item=service.item, generation=service.generation, cursor=start,
                    establishment=[task.operation for task in establishment],
                    deferred_service=[task.operation for task in dependent],
                    added_tasks=0, extra_hires=0, paid_prefix_preserved=True)
    return None
