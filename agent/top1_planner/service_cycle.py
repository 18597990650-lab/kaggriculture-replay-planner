"""Plan temporary feed pauses while retaining an observed animal and its visits."""
from dataclasses import replace
from functools import lru_cache

from . import rules
from .compiler import distance, nearest_shed
from .programmes import ProductionProgramme
from .task_allocation import Revision

PREFIX = 'cycle-pause:'


def service_required(task, tile, day):
    """A quoted pause is revoked when actual hunger makes recovery necessary."""
    if not task.lot or not task.lot.startswith(PREFIX) or task.operation not in ('FEED', 'CARE'):
        return True
    if not isinstance(tile, dict) or (tile.get('animal'), tile.get('placed_day')) != (task.item, task.generation):
        return True
    return day < 29 and tile['consecutive_unfed'] >= 1


def _advance(tile, day, feed, care, operations):
    farm = dict(tiles=[[dict(tile)]], farmer=[0, 0], hands=[])
    private = dict(inventories=[{'WHEAT': 1}], seeds={}, shed={})
    spent = int(feed and not tile['fed_today'])
    for op in operations:
        if op == 'FEED' and not feed or op == 'CARE' and not care:
            continue
        rules._apply_unit_action(farm, private, 0, [op], 1, day, 24, 100)
    goods = {i: n for i, n in private['inventories'][0].items() if i != 'WHEAT'}
    if day < 29:
        rules._daily_refresh_animals(farm, day)
    return farm['tiles'][0][0], spent, goods


def _plan(tile, start, schedule, price, arrival):
    """Biology and scheduled visits are exact; delivery prices are a shortlist model."""
    animal = tile['animal']
    @lru_cache(maxsize=None)
    def solve(day, state):
        if day == 30:
            return 0, ()
        current = dict(state)
        operations = schedule[day]
        feeds = (False,)
        if 'FEED' in operations and day < 29:
            feeds = (True, False)
        best = None
        for feed in feeds:
            cares = (False,)
            if 'CARE' in operations and (feed or current['fed_today']) and day < 29:
                cares = (True, False)
            for care in cares:
                future, spent, goods = _advance(current, day, feed, care, operations)
                if future.get('animal') != animal:
                    continue
                tail, choices = solve(day+1, tuple(sorted(future.items())))
                value = sum(price(i, arrival(day), q) for i, q in goods.items())
                value -= spent * (price('WHEAT', day*24, 1) + 2)
                value += tail
                if best is None or value > best[0]:
                    best = value, ((day, feed, care),) + choices
        return best if best is not None else (float('-inf'), ())

    current, baseline = dict(tile), 0
    for day in range(start, 30):
        current, spent, goods = _advance(current, day, 'FEED' in schedule[day], 'CARE' in schedule[day], schedule[day])
        if current.get('animal') != animal:
            return None
        baseline += sum(price(i, arrival(day), q) for i, q in goods.items())
        baseline -= spent * (price('WHEAT', day*24, 1) + 2)
    optimum, choices = solve(start, tuple(sorted(tile.items())))
    return dict(gain=optimum-baseline, days=choices, states=solve.cache_info().currsize)


def cycle_variant(programme, observation, controller, *, price):
    tick = int(observation['step'])
    if tick % 24 or not 216 <= tick <= 648:
        return None
    day = tick//24
    farm = observation['farms'][int(observation['player'])]
    replacements, plans = {}, []
    for y, row in enumerate(farm['tiles']):
        for x, tile in enumerate(row):
            if not isinstance(tile, dict) or not tile.get('animal'):
                continue
            point = x, y
            visits = [[t for actor, seq in sorted(actors.items()) for t in seq if t.point == point]
                      for actors in programme.days]
            # A pause never takes over a paid successor, establishment or declared exit.
            if any(t.lot or t.operation in ('PLACE', 'PLANT', 'CLEAR_CROP', 'DIG')
                   for seq in visits[day:] for t in seq):
                continue
            schedule = [tuple(t.operation for t in seq if t.operation in
                        ('FEED', 'CARE', 'HARVEST', 'COLLECT_FERTILIZER')) for seq in visits]
            travel = distance(point, nearest_shed(point)) + 4
            decision = _plan(tile, day, schedule, price, lambda d: min(718, d*24+travel))
            if decision is None or decision['gain'] <= 0:
                continue
            skipped = []
            for d, feed, care in decision['days']:
                for op, enabled in (('FEED', feed), ('CARE', care)):
                    if not enabled and op in schedule[d]:
                        replacements[(d, point, op)] = (tile['animal'], tile['placed_day'])
                        skipped.append((d, op))
            if skipped:
                plans.append(dict(point=point, item=tile['animal'], generation=tile['placed_day'],
                                  gain=decision['gain'], pauses=skipped, model_states=decision['states']))
    if not plans:
        return None
    days = []
    for d, actors in enumerate(programme.days):
        rows = {}
        for actor, seq in actors.items():
            changed = []
            for task in seq:
                key = d, task.point, task.operation
                if key in replacements:
                    animal, born = replacements[key]
                    task = replace(task, item=animal, generation=born,
                        lot=f'{PREFIX}{tick}:{task.point[0]}:{task.point[1]}:{animal}:{born}')
                changed.append(task)
            rows[actor] = tuple(changed)
        days.append(rows)
    candidate = ProductionProgramme(programme.name+f'-cycle-service-{tick}', tuple(days), programme.workers)
    if not controller.can_continue(candidate):
        raise ValueError('Cycle service changed an executed prefix')
    points = [r['point'] for r in plans]
    return Revision(candidate, dict(kind='cycle_service', mode='guarded_cycle_service', item='WHEAT',
        point=points[0], points=points, step=tick, estimate=sum(r['gain'] for r in plans),
        marginal_wages=0, retired_animals=[], added_plantings=[], added_herds=[], lots=[],
        removed_tasks=[], cancelled_points=[], work=[], service_plans=plans,
        price_basis='Scheduled-visit biology screen; real food, protected recovery, delivery and settlement require a complete native quote'))
