"""Complete an existing survival obligation when its owners cannot arrive today."""
from . import rules
from .compiler import distance
from .work_dispatch import _establishment_blocks, _ready


def take_critical_water(controller, actor, farm, private, market, tick):
    """Move only unpaid watering, retaining fertilizer and future crop visits."""
    day, hour = divmod(tick, 24)
    if day >= 29:
        return None
    rows = controller._tasks(day)
    recipient = rows.get(actor, ())
    if controller.cursor.get(actor, 0) < len(recipient):
        return None
    remaining = 24 - hour
    origin = tuple(rules._farmer_position(farm, actor))
    blocked = {task.point for owner, tasks in rows.items()
               for task in tasks[controller.cursor.get(owner, 0):]
               if task.operation in ('PLANT', 'PLACE', 'DIG', 'CLEAR_CROP', 'BUILD_PASTURE', 'BUILD_COOP')
               and _establishment_blocks(controller, task, farm)}
    obligations = {}
    for owner, tasks in rows.items():
        if owner == actor or owner > len(farm['hands']):
            continue
        for index in range(controller.cursor.get(owner, 0), len(tasks)):
            task = tasks[index]
            if task.operation != 'WATER' or task.point in blocked:
                continue
            tile = farm['tiles'][task.point[1]][task.point[0]]
            if not isinstance(tile, dict) or tile.get('crop') not in rules.CROPS:
                continue
            if (tile['watered_today'] or tile['consecutive_unwatered'] < 1
                    or task.item != tile['crop'] or task.generation != tile['planted_day']
                    or not _ready(task, tile, day)):
                continue
            obligations.setdefault(task.point, []).append((owner, index, task))
    candidates = []
    for point, owners in obligations.items():
        finish = distance(origin, point) + 1
        if finish > remaining:
            continue
        # A direct journey is a lower bound even if all prior work is skipped.
        # Use the same full remaining time for actors already moved this turn;
        # that makes the inability-to-arrive proof conservative.
        if any(distance(tuple(rules._farmer_position(farm, owner)), point) + 1 <= remaining
               for owner, _, _ in owners):
            continue
        tile = farm['tiles'][point[1]][point[0]]
        value = market['prices'][tile['crop']] * rules.CROPS[tile['crop']]['max_yield']
        candidates.append((value / finish, value, -finish, point, owners))
    if not candidates:
        return None
    _, _, _, point, owners = max(candidates, key=lambda row: row[:4])
    changed = dict(rows)
    for owner in {owner for owner, _, _ in owners}:
        removed = {index for source, index, _ in owners if source == owner}
        changed[owner] = tuple(task for index, task in enumerate(rows[owner]) if index not in removed)
    task = owners[0][2]
    changed[actor] = recipient + (task,)
    controller.day_sequences = changed
    if controller.calendar is not None:
        controller.calendar.sequences = dict(changed)
    return dict(kind='critical_water', step=tick, donor=owners[0][0],
                donors=sorted({owner for owner, _, _ in owners}), recipient=actor,
                point=point, item=task.item, generation=task.generation, operations=['WATER'],
                extra_hires=0, new_establishment=0, material_cost=0,
                deadline=(day + 1) * 24 - 1, remaining_steps=remaining)
