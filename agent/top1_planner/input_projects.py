"""Fund a fertilizer tour only for observable crops with a retained harvest."""
from . import rules
from .compiler import distance, nearest_shed
from .programmes import ProductionProgramme, Task
from .task_allocation import Revision, _Calendar


def _receipts(tile, start, schedule, fertilize):
    farm = dict(tiles=[[dict(tile)]], farmer=[0, 0], hands=[])
    private = dict(inventories=[{}], seeds={}, shed={})
    receipts = []
    item, born = tile['crop'], tile['planted_day']
    for day in range(start, min(30, start+6)):
        current = farm['tiles'][0][0]
        if not isinstance(current, dict) or (current.get('crop'), current.get('planted_day')) != (item, born):
            break
        if day == start and fertilize:
            private['inventories'][0]['FERTILIZER'] = 1
            rules._apply_unit_action(farm, private, 0, ['FERTILIZE'], 1, day, 24, 100)
        for op in schedule[day]:
            if op in ('PLANT', 'PLACE', 'DIG', 'CLEAR_CROP'):
                return tuple(receipts)
            if op == 'FERTILIZE':
                if current.get('fertilized_until_day', -1) >= day+2:
                    continue
                private['inventories'][0]['FERTILIZER'] = 1
            before = private['inventories'][0].get(item, 0)
            rules._apply_unit_action(farm, private, 0, [op], 1, day, 24, 100)
            gained = private['inventories'][0].get(item, 0)-before
            if gained:
                receipts.append((day, gained))
                return tuple(receipts)
        rules._daily_refresh_plants(farm, day, 24)
    return tuple(receipts)


def input_variant(programme, observation, controller, *, price, day_states=()):
    tick = int(observation['step'])
    if tick % 24 or not 216 <= tick <= 648:
        return None
    day = tick//24
    calendar = _Calendar(programme, observation, controller, day_states)
    candidates = []
    for y, row in enumerate(calendar.farm['tiles']):
        for x, tile in enumerate(row):
            if not isinstance(tile, dict) or tile.get('crop') not in ('WHEAT', 'CARROT', 'MELON'):
                continue
            if tile.get('fertilized_until_day', -1) >= day+2:
                continue
            point, item = (x, y), tile['crop']
            schedule = [tuple(t.operation for actor, seq in sorted(actors.items()) for t in seq
                              if t.point == point and t.operation in
                              ('WATER', 'HARVEST', 'FERTILIZE', 'DIG', 'CLEAR_CROP', 'PLANT', 'PLACE'))
                        for actors in programme.days]
            base, added = _receipts(tile, day, schedule, False), _receipts(tile, day, schedule, True)
            if not added or sum(q for d, q in added) <= sum(q for d, q in base):
                continue
            arrival = lambda d: min(718, d*24+distance(point, nearest_shed(point))+3)
            gain = sum(price(item, arrival(d), q) for d, q in added)-sum(price(item, arrival(d), q) for d, q in base)
            if gain > price('FERTILIZER', tick, 1)+2:
                candidates.append(dict(point=point, item=item, generation=tile['planted_day'],
                                       gross=gain, baseline=base, fertilized=added))
    if not candidates:
        return None
    # Keep one small closed tour; a quote accounts for travel, wages and other crops.
    targets, position = [], (4, 4)
    remaining = list(candidates)
    while remaining and len(targets) < 8:
        selected = max(remaining, key=lambda r: (r['gross']/(distance(position, r['point'])+1), r['point']))
        remaining.remove(selected)
        targets.append(selected)
        position = selected['point']
    days, workers = calendar.rows(), list(programme.workers)
    block = (Task(nearest_shed(targets[0]['point']), 'PICKUP', 'FERTILIZER', None),) + tuple(
        Task(r['point'], 'FERTILIZE', r['item'], r['generation'], f'input:{tick}:{r["point"][0]}:{r["point"][1]}') for r in targets)
    work = calendar.insert(days[day], workers, day, block, True)
    cost = price('FERTILIZER', tick, len(targets))+2*len(targets)+work['marginal_wage']
    estimate = sum(r['gross'] for r in targets)-cost
    if estimate <= 0 or work['estimated_overload']:
        return None
    candidate = ProductionProgramme(programme.name+f'-input-tour-{tick}', tuple(days), tuple(workers))
    if not controller.can_continue(candidate):
        raise ValueError('An input tour changed an executed prefix')
    points = [r['point'] for r in targets]
    return Revision(candidate, dict(kind='input_service', mode='funded_fertilizer_tour', item='FERTILIZER',
        point=points[0], points=points, step=tick, estimate=estimate, marginal_wages=work['marginal_wage'],
        retired_animals=[], added_plantings=[], added_herds=[], lots=[], removed_tasks=[], cancelled_points=[],
        work=[work], input_targets=targets,
        price_basis='Retained-harvest input screen; actor-order timing and realized output require complete native admission'))
