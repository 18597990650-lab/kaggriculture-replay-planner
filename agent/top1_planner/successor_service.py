"""Keep accepted successor service reachable before its daily deadline."""
from . import rules
from .compiler import distance, nearest_shed
from .redeployment import managed
from .service_cycle import service_required


def service_actors(controller, day):
    rows = controller._tasks(day)
    if rows is not controller.successor_service_rows:
        controller.successor_service_rows = rows
        controller.successor_service_actors = frozenset(actor for actor, sequence in rows.items()
            if any(managed(t) and t.operation in ('FEED', 'CARE') for t in sequence))
    return controller.successor_service_actors


def protect_successor_service(controller, actor, farm, private, day, tick):
    if day >= 29 or actor not in service_actors(controller, day):
        return None
    rows = controller._tasks(day)
    sequence = rows.get(actor, ())
    cursor = controller.cursor.get(actor, 0)
    remaining = sequence[cursor:]
    if not remaining:
        return None
    pos = tuple(rules._farmer_position(farm, actor))
    bag = private['inventories'][actor]
    available = min(24 - tick % 24, 719 - tick)
    lots = {}
    for index in range(cursor, len(sequence)):
        task = sequence[index]
        if not managed(task) or task.operation not in ('FEED', 'CARE'):
            continue
        tile = farm['tiles'][task.point[1]][task.point[0]]
        if not controller._successor_matches(task, tile) or not service_required(task, tile, day):
            continue
        fulfilled = tile['fed_today'] if task.operation == 'FEED' else tile['cared_today']
        if fulfilled:
            continue
        lots.setdefault(task.lot, []).append((index, task))
    candidates = []
    for lot, pairs in lots.items():
        pairs.sort(key=lambda pair: (pair[1].operation != 'FEED', pair[0]))
        point = pairs[0][1].point
        travel = distance(pos, point)
        needs_feed = any(t.operation == 'FEED' for _, t in pairs)
        if needs_feed and not bag.get('WHEAT', 0):
            if not private['shed'].get('WHEAT', 0):
                continue
            shed = nearest_shed(pos)
            travel = distance(pos, shed) + 1 + distance(shed, point)
        finish = travel + len(pairs)
        if finish + 1 < available or finish > available:
            continue
        if tuple(t for _, t in pairs) == remaining[:len(pairs)]:
            continue
        candidates.append((available - finish, pairs[0][0], lot, pairs))
    if not candidates:
        return None
    _, _, lot, pairs = min(candidates)
    indices = {index for index, _ in pairs}
    advanced = tuple(task for _, task in pairs)
    changed = dict(rows)
    changed[actor] = sequence[:cursor] + advanced + tuple(
        task for index, task in enumerate(sequence[cursor:], cursor) if index not in indices)
    controller.day_sequences = changed
    if controller.calendar is not None:
        controller.calendar.sequences = dict(changed)
    return dict(kind='successor_service_deadline', step=tick, actor=actor, lot=lot,
        point=advanced[0].point, operations=[t.operation for t in advanced],
        added_tasks=0, extra_hires=0, paid_prefix_preserved=True)
