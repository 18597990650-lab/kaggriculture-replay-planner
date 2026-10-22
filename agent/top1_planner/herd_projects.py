"""Complete small pasture investments with daily crew and delivery requirements."""
from . import rules
from .compiler import nearest_shed
from .programmes import ProductionProgramme, Task
from .task_allocation import Revision, _Calendar
from .redeployment import _animal_receipts, _animal_value


def _points(quadrant):
    return tuple((9-x if quadrant in ('NW', 'SW') else x,
                  9-y if quadrant in ('NW', 'NE') else y)
                 for y in (5, 6) for x in (5, 6, 7))


def _candidate(programme, observation, controller, item, quadrant, price, day_states):
    calendar = _Calendar(programme, observation, controller, day_states)
    start = calendar.day
    if start+rules.ANIMALS[item]['first_yield_day']+rules.ANIMALS[item]['interval'] > 29:
        return None
    points = _points(quadrant)
    farm = calendar.farm
    for x, y in points:
        tile = farm['tiles'][y][x]
        if tile is not None and tile != 'LOCKED' and not (
                isinstance(tile, dict) and (tile.get('kind') == 'WEED' or
                tile.get('kind') == rules.ANIMALS[item]['structure'] and not tile.get('animal'))):
            return None
    if any(t.point in points and t.operation not in ('PICKUP', 'DEPOSIT', 'DROP')
           for actors in programme.days[start:] for seq in actors.values() for t in seq):
        return None
    remaining = [q for q in rules.LAND_ORDER if q not in farm['unlocked_quadrants']]
    locked = quadrant not in farm['unlocked_quadrants']
    if locked and (not remaining or remaining[0] != quadrant):
        return None
    land = rules.LAND_PRICES[len(farm['unlocked_quadrants'])-1] if locked else 0
    estimate = sum(_animal_value(p, item, start, start, price) for p in points)-land
    if estimate <= 0:
        return None
    lots = [dict(point=p, item=item, generation=start, end=29,
                 lot=f'redeploy:{calendar.start}:{p[0]}:{p[1]}:{item}:{start}') for p in points]
    identities = {r['point']:r['lot'] for r in lots}
    product = rules.ANIMALS[item]['product']
    receipts = {d:(yielded, manure) for d, yielded, manure, feed in _animal_receipts(item, start, start)}
    days, workers = calendar.rows(), list(programme.workers)
    work, forgone_manure = [], 0
    for day in range(start, 30):
        ready, manure = receipts.get(day, (0, 0))
        alternatives = []
        for collect in (True, False):
            for size in (6, 3):
                rows, staffing = dict(days[day]), list(workers)
                changes = []
                for begin in range(0, len(points), size):
                    group = points[begin:begin+size]
                    block = []
                    if day < 29:
                        block.append(Task(nearest_shed(group[0]), 'PICKUP', 'WHEAT', None))
                    for point in group:
                        lot = identities[point]
                        if day == start:
                            block.extend((Task(point, 'BUILD_'+rules.ANIMALS[item]['structure'], item, start, lot),
                                          Task(point, 'PLACE', item, start, lot)))
                        block.append(Task(point, 'HARVEST', item, start, lot))
                        if day < 29:
                            block.extend(Task(point, op, item, start, lot) for op in ('FEED', 'CARE'))
                        if collect:
                            block.append(Task(point, 'COLLECT_FERTILIZER', item, start, lot))
                    if ready:
                        block.append(Task(nearest_shed(group[-1]), 'DEPOSIT', product, None))
                    if collect and manure:
                        block.append(Task(nearest_shed(group[-1]), 'DEPOSIT', 'FERTILIZER', None))
                    # Absent harvests/manure need no time in this daily crew screen.
                    block = tuple(t for t in block if not (t.operation == 'HARVEST' and not ready)
                                  and not (t.operation == 'COLLECT_FERTILIZER' and not manure))
                    if block:
                        changes.append(calendar.insert(rows, staffing, day, block, True))
                lost = 0 if collect else price('FERTILIZER', min(718, day*24+22), len(points)*manure)
                score = (sum(r['estimated_overload'] for r in changes),
                         sum(r['marginal_wage'] for r in changes)+lost, -int(collect), -size)
                alternatives.append((score, rows, staffing, changes, lost))
        score, days[day], workers, changes, lost = min(alternatives, key=lambda r:r[0])
        if score[0]:
            return None
        work.extend(changes)
        forgone_manure += lost
    wages = sum(r['marginal_wage'] for r in work)
    estimate -= wages+forgone_manure
    if estimate <= 0:
        return None
    candidate = ProductionProgramme(programme.name+f'-pasture-{calendar.start}-{quadrant.lower()}-{item.lower()}6',
                                    tuple(days), tuple(workers))
    if not controller.can_continue(candidate):
        raise ValueError('Pasture investment changed an executed prefix')
    return Revision(candidate, dict(kind='productive_redeployment', mode='regional_herd_project',
        item=item, point=points[0], points=points, step=calendar.start, estimate=estimate,
        marginal_wages=wages, land_cost=land, forgone_manure=forgone_manure, fertilized=False,
        retired_animals=[], added_plantings=[], added_herds=lots, lots=lots,
        removed_tasks=[], cancelled_points=[], work=work,
        price_basis='Complete six-animal investment and daily crew screen; full native execution and settlement determine admission'))


def pasture_variant(programme, observation, controller, *, price, day_states=()):
    tick = int(observation['step'])
    if tick % 24 or not 216 <= tick <= 432:
        return None
    candidates = []
    for quadrant in ('SE', 'SW', 'NE', 'NW'):
        for item in ('SHEEP', 'COW', 'GOOSE'):
            candidate = _candidate(programme, observation, controller, item, quadrant, price, day_states)
            if candidate is not None:
                candidates.append(candidate)
    return max(candidates, key=lambda r:(r.detail['estimate'], r.programme.name), default=None)
