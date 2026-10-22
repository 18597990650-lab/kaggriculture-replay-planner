"""Generate complete regional investments and same-pasture herd transitions."""
from collections import Counter
from dataclasses import replace
from functools import lru_cache
import json
from pathlib import Path

from . import rules
from .compiler import distance, nearest_shed
from .programmes import ProductionProgramme, Task
from .task_allocation import Revision, _Calendar, _remove, _site_loss
from .crop_input_value import crop_input_value
from .redeployment import release_day, _animal_value

HERD_WORK = {'FEED', 'CARE', 'HARVEST', 'COLLECT_FERTILIZER'}


@lru_cache(maxsize=1)
def _tomato_template():
    data = json.loads((Path(__file__).resolve().parents[1] / 'project_library/regional_tomato.json').read_bytes())
    if data['item'] != 'TOMATO' or len(data['days']) != 12 or len(data['points']) != 20:
        raise ValueError('The regional project requires a complete twelve-day crop recipe')
    return data


def _point(point, quadrant):
    x, y = point
    return (9-x if quadrant in ('NW', 'SW') else x,
            9-y if quadrant in ('NW', 'NE') else y)


def _regional(programme, observation, controller, quadrant, price, day_states):
    calendar = _Calendar(programme, observation, controller, day_states)
    start = calendar.day
    template = _tomato_template()
    points = tuple(_point(p, quadrant) for p in template['points'])
    farm = calendar.farm
    clear = {}
    for point in points:
        tile = farm['tiles'][point[1]][point[0]]
        if isinstance(tile, dict) and tile.get('animal'):
            return None
        if tile is not None and tile != 'LOCKED' and not (
                isinstance(tile, dict) and (tile.get('crop') or tile.get('kind') == 'WEED')):
            return None
        if isinstance(tile, dict) and tile.get('crop'):
            clear[point] = tile['crop'], tile['planted_day']
    if any(t.point in points and t.operation in ('PLACE', 'BUILD_COOP', 'BUILD_PASTURE')
           for actors in programme.days[start:] for seq in actors.values() for t in seq):
        return None
    locked = quadrant not in farm['unlocked_quadrants']
    remaining = [q for q in rules.LAND_ORDER if q not in farm['unlocked_quadrants']]
    if locked and (not remaining or quadrant != remaining[0]):
        return None
    land_cost = rules.LAND_PRICES[len(farm['unlocked_quadrants'])-1] if locked else 0
    estimate = sum(crop_input_value(p, 'TOMATO', ((start, start+11),), price, True)
                   - _site_loss(programme, p, calendar.start, 30, observation, price) for p in points) - land_cost
    if estimate <= 0:
        return None
    days, workers = calendar.rows(), list(programme.workers)
    removed = [row for point in points for row in _remove(days, calendar, point, start, 30)]
    lots = [dict(point=p, item='TOMATO', generation=start, end=start+11,
                 lot=f'redeploy:{calendar.start}:{p[0]}:{p[1]}:TOMATO:{start}') for p in points]
    identities = {row['point']: row['lot'] for row in lots}
    work = []
    for offset, roles in enumerate(template['days']):
        day = start + offset
        for role in roles:
            block = []
            if any(t['operation'] == 'FERTILIZE' for t in role):
                block.append(Task(nearest_shed(_point(role[0]['point'], quadrant)), 'PICKUP', 'FERTILIZER', None))
            for row in role:
                point, op = _point(row['point'], quadrant), row['operation']
                if op == 'PLANT' and point in clear:
                    item, generation = clear[point]
                    block.append(Task(point, 'CLEAR_CROP', item, generation))
                block.append(Task(point, op, 'TOMATO', start, identities[point]))
            if any(t.operation == 'HARVEST' for t in block):
                block.append(Task(nearest_shed(block[-1].point), 'DEPOSIT', 'TOMATO', None))
            if block:
                work.append(calendar.insert(days[day], workers, day, tuple(block), True))
    wage = sum(row['marginal_wage'] for row in work)
    if estimate <= wage:
        return None
    target = ProductionProgramme(programme.name+f'-region-{calendar.start}-{quadrant.lower()}-tomato20',
                                 tuple(days), tuple(workers))
    if not controller.can_continue(target):
        raise ValueError('Regional project changed an executed prefix')
    return Revision(target, dict(kind='productive_redeployment', mode='regional_crop_project',
        item='TOMATO', point=points[0], points=points, step=calendar.start, estimate=estimate-wage,
        fertilized=True, marginal_wages=wage, land_cost=land_cost, retired_animals=[], added_herds=[],
        added_plantings=lots, lots=lots, removed_tasks=removed, cancelled_points=points, work=work,
        price_basis='Whole crop project shortlist; actual procurement, displaced production and settlement are quoted'))


def _retarget(programme, observation, controller, points, destination, price, day_states,
              *, require_positive=True):
    calendar = _Calendar(programme, observation, controller, day_states)
    start = calendar.day
    farm = calendar.farm
    old = {p: farm['tiles'][p[1]][p[0]] for p in points}
    if any(t.operation in ('PLANT', 'PLACE', 'BUILD_COOP', 'BUILD_PASTURE') and t.point in old
           for actors in programme.days[start:] for seq in actors.values() for t in seq):
        return None
    released = {p: release_day(tile, start) for p, tile in old.items()}
    if any(day+rules.ANIMALS[destination]['first_yield_day'] > 29 for day in released.values()):
        return None
    lots = [dict(point=p, item=destination, generation=released[p], end=29,
                 lot=f'redeploy:{calendar.start}:{p[0]}:{p[1]}:{destination}:{released[p]}') for p in points]
    identities = {row['point']: row['lot'] for row in lots}
    days, workers = calendar.rows(), list(programme.workers)
    removed, work = [], []
    for day in range(start, 30):
        seen = set()
        owners = set()
        for actor, sequence in list(days[day].items()):
            revised = []
            affected = False
            for task in sequence:
                if task.point not in old or task.operation not in HERD_WORK:
                    revised.append(task)
                    continue
                point = task.point
                removed.append(dict(day=day, actor=actor, operation=task.operation,
                                    item=task.item, generation=task.generation))
                if point in seen:
                    continue
                seen.add(point)
                if day < released[point]:
                    revised.extend((Task(point, 'HARVEST', old[point]['animal'], old[point]['placed_day']),
                                    Task(point, 'COLLECT_FERTILIZER', old[point]['animal'], old[point]['placed_day'])))
                else:
                    affected = True
                    owners.add(actor)
                    revised.append(Task(point, 'HARVEST', destination, released[point], identities[point]))
                    if day < 29:
                        revised.extend(Task(point, op, destination, released[point], identities[point]) for op in ('FEED', 'CARE'))
                    revised.append(Task(point, 'COLLECT_FERTILIZER', destination, released[point], identities[point]))
            if affected:
                # Preserve the donor's original drop and resource return order.
                # A new product obtains its own settlement visit as well.
                revised.append(Task(nearest_shed(points[0]), 'DEPOSIT', rules.ANIMALS[destination]['product'], None))
            days[day][actor] = tuple(revised)
        # Each live replacement gets service even when the donor calendar skips
        # that plot; its extra crew and delivery work belongs to this project.
        for p in points:
            if day < released[p] or day >= 29 or p in seen:
                continue
            lot = identities[p]
            block = (Task(nearest_shed(p), 'PICKUP', 'WHEAT', None),
                     Task(p, 'HARVEST', destination, released[p], lot),
                     Task(p, 'FEED', destination, released[p], lot),
                     Task(p, 'CARE', destination, released[p], lot),
                     Task(p, 'COLLECT_FERTILIZER', destination, released[p], lot),
                     Task(nearest_shed(p), 'DEPOSIT', rules.ANIMALS[destination]['product'], None),
                     Task(nearest_shed(p), 'DEPOSIT', 'FERTILIZER', None))
            added = calendar.insert(days[day], workers, day, block, True)
            if added['estimated_overload']:
                return None
            work.append(added)
        establishing = [p for p in points if day == released[p]]
        if establishing:
            ordered = sorted(establishing, key=lambda p: (distance(p, nearest_shed(p)), p))
            for begin in range(0, len(ordered), 2):
                group = ordered[begin:begin+2]
                block = [Task(nearest_shed(group[0]), 'PICKUP', destination, None),
                         Task(nearest_shed(group[0]), 'PICKUP', 'WHEAT', None)]
                for p in group:
                    if old[p]['kind'] != rules.ANIMALS[destination]['structure']:
                        block.extend((Task(p, 'DIG', None, None),
                                      Task(p, 'BUILD_'+rules.ANIMALS[destination]['structure'],
                                           destination, day, identities[p])))
                    block.extend((Task(p, 'PLACE', destination, day, identities[p]),
                                  Task(p, 'FEED', destination, day, identities[p]),
                                  Task(p, 'CARE', destination, day, identities[p])))
                added = calendar.insert(days[day], workers, day, tuple(block), True)
                if added['estimated_overload']:
                    return None
                work.append(added)
    wage = sum(row['marginal_wage'] for row in work)
    estimate = sum(_animal_value(p, destination, released[p], released[p], price)
                   - _animal_value(p, tile['animal'], tile['placed_day'], start, price, tile)
                   for p, tile in old.items()) - wage
    if require_positive and estimate <= 0:
        return None
    target = ProductionProgramme(programme.name+f'-herd-project-{calendar.start}-'+
        '-'.join(f'{p[0]}.{p[1]}' for p in points)+'-'+destination.lower(), tuple(days), tuple(workers))
    if not controller.can_continue(target):
        raise ValueError('Herd conversion changed an executed prefix')
    return Revision(target, dict(kind='productive_redeployment', mode='inherited_herd_project',
        item=destination, point=points[0], points=points, step=calendar.start, estimate=estimate,
        fertilized=False, marginal_wages=wage,
        retired_animals=[dict(point=p, item=old[p]['animal'], generation=old[p]['placed_day'],
                              release_day=released[p]) for p in points],
        added_plantings=[], added_herds=lots, lots=lots, removed_tasks=removed, cancelled_points=points,
        work=work, price_basis='Paid herd conversion including structure changes, inherited service, funded gaps and complete native admission'))


def project_variants(programme, observation, controller, *, price, day_states=()):
    """Expose complete investment and service families to the same native admission."""
    tick = int(observation['step'])
    if tick % 24 or not 216 <= tick <= 576:
        return ()
    day = tick//24
    farm = observation['farms'][int(observation['player'])]
    result = []
    if day <= 18:
        regions = [_regional(programme, observation, controller, q, price, day_states)
                   for q in ('SE', 'SW', 'NE', 'NW')]
        regions = [r for r in regions if r is not None]
        if regions:
            result.append(max(regions, key=lambda r: (r.detail['estimate'], r.programme.name)))
    herds = []
    for source, destination in ((s, d) for s in rules.ANIMALS for d in rules.ANIMALS if s != d):
        sites = [(x, y) for y, row in enumerate(farm['tiles']) for x, tile in enumerate(row)
                 if isinstance(tile, dict) and tile.get('animal') == source]
        gains = []
        for p in sites:
            tile = farm['tiles'][p[1]][p[0]]
            first = release_day(tile, day)
            if first+rules.ANIMALS[destination]['first_yield_day'] > 29:
                continue
            gain = _animal_value(p, destination, first, first, price)-_animal_value(p, source, tile['placed_day'], day, price, tile)
            if gain > 0:
                gains.append((gain, p))
        ranked = [p for gain, p in sorted(gains, key=lambda r: (-r[0], distance(r[1], nearest_shed(r[1])), r[1]))]
        sizes = sorted({min(k, len(sites), len(ranked)) for k in (1, 2, 4, 6, 8)}-{0})
        for count in sizes:
            points = tuple(ranked[:count])
            candidate = _retarget(programme, observation, controller, points, destination, price, day_states)
            if candidate is not None:
                herds.append(candidate)
    result.extend(herds)
    from .herd_projects import pasture_variant
    from .input_projects import input_variant
    from .service_cycle import cycle_variant
    from .crop_calendar import calendar_variants
    for candidate in (
            pasture_variant(programme, observation, controller, price=price, day_states=day_states),
            input_variant(programme, observation, controller, price=price, day_states=day_states),
            cycle_variant(programme, observation, controller, price=price)):
        if candidate is not None:
            result.append(candidate)
    calendars = calendar_variants(programme, observation, controller, price=price, day_states=day_states)
    # Every generated exposure is a complete paid-prefix-compatible calendar.
    # A larger biological estimate cannot replace executable smaller cohorts.
    result.extend(calendars)
    return tuple(sorted(result, key=lambda r: (-r.detail['estimate'], r.programme.name)))
