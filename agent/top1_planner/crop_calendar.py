"""Reprice consecutive unpaid short-crop cohorts on the incumbent work calendar."""
from collections import defaultdict
from dataclasses import replace
from functools import lru_cache

from . import rules
from .compiler import distance, nearest_shed
from .programmes import ProductionProgramme
from .task_allocation import Revision


_WORK = {'PLANT', 'WATER', 'FERTILIZE', 'HARVEST', 'DIG', 'CLEAR_CROP'}
_CROPS = ('WHEAT', 'CARROT')


@lru_cache(maxsize=512)
def _receipts(item, generation, service):
    """Use native crop biology with the retained service order and harvest age."""
    farm = dict(tiles=[[None]], farmer=[0, 0], hands=[])
    private = dict(inventories=[{}], seeds={item:1}, shed={})
    receipts = []
    spent = 0
    last_day = service[-1][0]
    for day in range(generation, last_day+1):
        for visit, op in service:
            if visit != day:
                continue
            if op == 'FERTILIZE':
                private['inventories'][0]['FERTILIZER'] = 1
            before = private['inventories'][0].get(item, 0)
            rules._apply_unit_action(farm, private, 0,
                ['PLANT', item] if op == 'PLANT' else [op], 1, day, 24, 100)
            quantity = private['inventories'][0].get(item, 0)-before
            if quantity:
                receipts.append((day, quantity))
            if op == 'FERTILIZE' and not private['inventories'][0].get('FERTILIZER', 0):
                spent += 1
        rules._daily_refresh_plants(farm, day, 24)
    return tuple(receipts), spent


def calendar_variants(programme, observation, controller, *, price, day_states=()):
    """Change whole finite cohorts, retaining every hand, visit and next planting.

    A six-day planting horizon can include several successive crops on each
    field. Existing paid generations and unrelated long-lived projects stay
    in their own queues. Native full-tail admission charges feed replacement,
    fertilizer, transport, market impact and all affected later crops.
    """
    tick = int(observation['step'])
    if tick % 24 or not 216 <= tick <= 600:
        return ()
    day = tick//24
    farm = observation['farms'][int(observation['player'])]
    predicted = {r['step']//24:r['farm'] for r in day_states}
    predicted[day] = farm
    cohorts = defaultdict(list)
    for d in range(day, 30):
        for actor, tasks in sorted(programme.days[d].items()):
            for task in tasks:
                if task.item in _CROPS and task.operation in _WORK:
                    cohorts[(task.point, task.item, task.generation)].append((d, task))
    opportunities = {item:defaultdict(list) for item in _CROPS}
    for key, visits in cohorts.items():
        point, old, generation = key
        if generation is None or not day <= generation <= min(27, day+5):
            continue
        births = [(d,t) for d,t in visits if t.operation == 'PLANT']
        if len(births) != 1 or births[0][0] != generation:
            continue
        if any(t.lot and not t.lot.startswith('redeploy:rotation:') for d,t in visits):
            continue
        tile = farm['tiles'][point[1]][point[0]]
        if isinstance(tile, dict) and (tile.get('animal') or tile.get('planted_day', -1) >= generation):
            continue
        available = predicted.get(generation)
        if available is not None:
            previous = available['tiles'][point[1]][point[0]]
            if isinstance(previous, dict) and previous.get('crop'):
                data = rules.CROPS[previous['crop']]
                age = generation-previous['planted_day']
                if data['ongoing'] and age < data['first_yield_day']+(data['max_yield']-1)*data['interval']:
                    continue
                if not data['ongoing'] and age < data['first_yield_day']:
                    continue
            if isinstance(previous, dict) and previous.get('animal'):
                continue
        harvests = [d for d,t in visits if t.operation == 'HARVEST']
        if len(harvests) != 1 or not 2 <= harvests[0]-generation <= 4:
            continue
        harvest = harvests[0]
        service = tuple((d,t.operation) for d,t in visits if d <= harvest)
        destination = 'CARROT' if old == 'WHEAT' else 'WHEAT'
        before, old_fert = _receipts(old, generation, service)
        after, new_fert = _receipts(destination, generation, service)
        if not before or not after:
            continue
        arrival = lambda d:min(718, (d+1)*24)
        # The shortlist uses guaranteed overnight delivery rather than
        # pretending a worker is already at the warehouse after harvesting.
        old_cash = sum(price(old, arrival(d), q) for d,q in before)-rules.CROPS[old]['seed']
        new_cash = sum(price(destination, arrival(d), q) for d,q in after)-rules.CROPS[destination]['seed']
        gain = new_cash-old_cash-(new_fert-old_fert)*price('FERTILIZER', generation*24)
        if gain > 0:
            opportunities[destination][point].append(dict(key=key, point=point, item=destination,
                generation=generation, end=harvest, estimate=gain,
                original_item=old, original_yield=sum(q for d,q in before),
                planned_yield=sum(q for d,q in after),
                lot=f'redeploy:rotation:{tick}:{point[0]}:{point[1]}:{destination}:{generation}'))
    result = []
    for item, sites in opportunities.items():
        ranked = sorted(sites, key=lambda p:(-sum(r['estimate'] for r in sites[p]),
                                            distance(p, nearest_shed(p)), p))
        seen = set()
        for count in (2, 4, 8):
            points = tuple(ranked[:count])
            if not points or points in seen:
                continue
            seen.add(points)
            lots = [r for point in points for r in sites[point]]
            selected = {r['key']:r for r in lots}
            days = []
            for d, actors in enumerate(programme.days):
                rows = {}
                for actor, seq in actors.items():
                    revised = []
                    for task in seq:
                        lot = selected.get((task.point, task.item, task.generation))
                        if d >= day and lot is not None and task.operation in _WORK:
                            task = replace(task, item=item, lot=lot['lot'])
                        revised.append(task)
                    rows[actor] = tuple(revised)
                days.append(rows)
            candidate = ProductionProgramme(programme.name+f'-rotation-{tick}-{item.lower()}-{len(points)}',
                                             tuple(days), programme.workers)
            if not controller.can_continue(candidate):
                raise ValueError('Crop calendar revision changed an executed prefix')
            public_lots = [{k:v for k,v in r.items() if k not in ('key', 'estimate')} for r in lots]
            result.append(Revision(candidate, dict(kind='rotation_batch', mode='continuous_short_crop_calendar',
                item=item, point=points[0], points=points, step=tick,
                estimate=sum(r['estimate'] for r in lots), marginal_wages=0, retired_animals=[],
                added_plantings=public_lots, added_herds=[], lots=public_lots,
                removed_tasks=[], cancelled_points=[], work=[],
                price_basis='Consecutive unpaid crop cohorts retain service and successor slots; the full native tail charges feed purchases, material opportunity cost, settlement and displaced output')))
    return tuple(sorted(result, key=lambda r:(-r.detail['estimate'], r.programme.name)))
