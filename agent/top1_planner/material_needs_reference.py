"""Reference material scan using the original compiler methods."""
from collections import Counter
from . import rules
from .compiler import distance


def gather(controller, farm, private, day):
    seeds, animals = {}, {}
    reserves = Counter()
    locked = False
    for actor in range(controller.programme.workers[day] + 1):
        sequence = controller._tasks(day).get(actor, ())
        if controller.cursor.get(actor, 0) >= len(sequence):
            continue
        inputs = controller._warehouse_inputs(actor, farm, private, day)
        if inputs:
            reserves.update(inputs)
        cached = controller._procurement_sequences.get(actor)
        if cached is None or cached[0] is not sequence:
            prefix = [0]
            for previous, task in zip(sequence, sequence[1:]):
                prefix.append(prefix[-1]+distance(previous.point, task.point)+1)
            entries = tuple((index, task) for index, task in enumerate(sequence)
                if task.operation in ('PLANT', 'PLACE', 'BUILD_PASTURE', 'BUILD_COOP'))
            cached = controller._procurement_sequences[actor] = (sequence, entries, prefix)
        cursor = controller.cursor.get(actor, 0)
        if cursor >= len(sequence) or not cached[1]:
            continue
        pos = tuple(rules._farmer_position(farm, actor)) if actor <= len(farm['hands']) else (4, 4)
        entry_cost = distance(pos, sequence[cursor].point)+1-cached[2][cursor]
        for index, task in cached[1]:
            if index < cursor:
                continue
            eta = entry_cost+cached[2][index]
            tile = controller._tile(farm, task)
            if (task.operation == 'PLANT' and day+rules.CROPS[task.item]['first_yield_day']<=29
                    and not controller._established(task,tile)):
                seeds.setdefault((task.point, task.item), eta)
            if task.operation == 'PLACE' and task.item in rules.ANIMALS and not controller._established(task,tile):
                animals.setdefault((task.point, task.item), eta)
            if tile == 'LOCKED' and task.operation in ('PLANT', 'PLACE', 'BUILD_PASTURE', 'BUILD_COOP'):
                locked = True
    return seeds,animals,reserves,locked
