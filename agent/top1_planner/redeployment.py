"""Replace an occupied plot with a complete, physically executable successor."""
from functools import lru_cache

from . import rules
from .compiler import distance, nearest_shed
from .programmes import ProductionProgramme, Task
from .task_allocation import Revision, _Calendar, _end_day, _site_loss
from .crop_input_value import crop_service,crop_input_value

SITE_WORK = {'DIG', 'CLEAR_CROP', 'PLANT', 'WATER', 'FERTILIZE', 'HARVEST',
             'BUILD_COOP', 'BUILD_PASTURE', 'PLACE', 'FEED', 'CARE', 'COLLECT_FERTILIZER'}


def release_day(tile, day):
    """First empty morning after stopping feed, including today's paid feeding."""
    if not isinstance(tile, dict) or not tile.get('animal'):
        return day
    return day + (3 if tile['fed_today'] else 2 - tile['consecutive_unfed'])


def managed(task):
    return bool(task.lot and task.lot.startswith('redeploy:'))


def delivery_uncertainty_buffer(observation, result, detail):
    """Require room for one missed product unit per replaced plot.

    This is an admission allowance, not a bound on future price or execution
    error. Both currently visible price and the quoted first-delivery price
    contribute; a planned mean-demand recovery cannot make the allowance zero.
    """
    inventories={row['step']+1:row['market_inventory'] for row in result['records']}
    start=int(observation['step'])
    inventories[start]=observation['market']['inventory']
    values={}
    for lot in detail['lots']:
        item=lot['item']
        data=rules.ANIMALS[item] if item in rules.ANIMALS else rules.CROPS[item]
        product=data['product'] if item in rules.ANIMALS else item
        binding=result.get('lot_bindings',{}).get(lot['lot'])
        generation=binding[2] if binding else lot['generation']
        point=tuple(lot['point'])
        arrival=min(718,max(start,(generation+data['first_yield_day'])*24+distance(point,nearest_shed(point))+4))
        price=rules.market_price(product,inventories[arrival][product],observation['market'].get('params'))
        values[point]=max(values.get(point,0),price,observation['market']['prices'][product])
    return sum(values.values())


def identity(task, tile):
    """A successor service task cannot bind to the animal or crop it displaced."""
    if not isinstance(tile, dict):
        return None
    key, generation = ('animal', 'placed_day') if task.item in rules.ANIMALS else ('crop', 'planted_day')
    if tile.get(key) == task.item and tile.get(generation, -1) >= task.generation:
        return task.item, tile[generation]
    return None


def crop_cycles(item, start, point, *, period=None):
    if period is not None and (item not in ('WHEAT','CARROT') or period not in (2,3,4)):
        raise ValueError('Short crop turnover requires a two-, three- or four-day crop')
    generations = []
    while start + rules.CROPS[item]['first_yield_day'] <= 29:
        end = min(29, start+period if period is not None else _end_day(item, start))
        if end==29 and not rules.CROPS[item]['ongoing'] and start+rules.CROPS[item]['first_yield_day']<=28:
            end=28
        if end * 24 + distance(point, nearest_shed(point)) + 4 > 718:
            break
        generations.append((start, end))
        if rules.CROPS[item]['ongoing']:
            break
        start = end if period is not None else end + 1
    return generations


@lru_cache(maxsize=512)
def _animal_receipts(item, generation, start, paid_state=None):
    """Biological screen including manure and feed; native execution is admission."""
    tile = dict(paid_state) if paid_state is not None else rules._new_animal(item, generation)
    farm = dict(tiles=[[tile]], farmer=[0, 0], hands=[])
    private = dict(inventories=[{}], shed={}, seeds={})
    rows = []
    product = rules.ANIMALS[item]['product']
    for day in range(start, 30):
        tile = farm['tiles'][0][0]
        if not isinstance(tile, dict) or tile.get('animal') != item:
            break
        harvest = tile.get('yield_units', 0)
        fertilizer = int(tile.get('fertilizer_available', False))
        feed = int(day < 29 and not tile['fed_today'])
        rows.append((day, harvest, fertilizer, feed))
        rules._apply_unit_action(farm, private, 0, ['HARVEST'], 1, day, 24, 100)
        rules._apply_unit_action(farm, private, 0, ['COLLECT_FERTILIZER'], 1, day, 24, 100)
        if day < 29:
            private['inventories'][0]['WHEAT'] = 1
            rules._apply_unit_action(farm, private, 0, ['FEED'], 1, day, 24, 100)
            rules._apply_unit_action(farm, private, 0, ['CARE'], 1, day, 24, 100)
        rules._daily_refresh_animals(farm, day)
    return tuple(rows)


def _animal_value(point, item, generation, start, price, tile=None):
    net = -rules.ANIMALS[item]['cost'] if tile is None else 0
    travel = distance(point, nearest_shed(point)) + 5
    product = rules.ANIMALS[item]['product']
    state = None if tile is None else tuple(sorted(tile.items()))
    for day, harvest, fertilizer, feed in _animal_receipts(item, generation, start, state):
        arrival = day * 24 + travel
        net += price(product, arrival, harvest) + price('FERTILIZER', arrival, fertilizer)
        net -= feed * price('WHEAT', day * 24, 1)
    return net


def _remove_site(days, calendar, point):
    removed = []
    for day in range(calendar.day, 30):
        for actor, sequence in list(days[day].items()):
            prefix = calendar.cursor(day, actor)
            keep = []
            for index, task in enumerate(sequence):
                if index >= prefix and task.point == point and task.operation in SITE_WORK:
                    removed.append(dict(day=day, actor=actor, operation=task.operation,
                                        item=task.item, generation=task.generation))
                else:
                    keep.append(task)
            days[day][actor] = tuple(keep)
    return removed


def build_revision(programme, observation, controller, points, item, *,
                   day_states=(), allow_hire=False, estimate=0, fertilized=False, crop_period=None):
    """Stop the old site, salvage it, establish and service the successor to 719."""
    if crop_period is not None and (item not in ('WHEAT','CARROT') or crop_period not in (2,3,4)):
        raise ValueError('Short crop turnover requires a two-, three- or four-day crop')
    calendar = _Calendar(programme, observation, controller, day_states)
    day, tick = calendar.day, calendar.start
    if tick % 24:
        raise ValueError('Redeployment admission requires an unpaid day boundary')
    days, workers = calendar.rows(), list(programme.workers)
    visits = {d: [] for d in range(day, 30)}
    retired, crops, herds, lots, removed = [], [], [], [], []
    for point in points:
        point = tuple(point)
        tile = calendar.farm['tiles'][point[1]][point[0]]
        if not isinstance(tile, dict) or not (tile.get('crop') or tile.get('animal')):
            raise ValueError('Redeployment requires an observed occupied plot')
        first = release_day(tile, day)
        removed.extend(_remove_site(days, calendar, point))
        if tile.get('animal'):
            old_item, generation = tile['animal'], tile['placed_day']
            retired.append(dict(point=point, item=old_item, generation=generation, release_day=first))
            for d in range(day, min(first, 30)):
                visits[d].extend((Task(point, 'HARVEST', old_item, generation),
                                  Task(point, 'COLLECT_FERTILIZER', old_item, generation),
                                  Task(nearest_shed(point), 'DEPOSIT', rules.ANIMALS[old_item]['product'], None),
                                  Task(nearest_shed(point), 'DEPOSIT', 'FERTILIZER', None)))
        else:
            # This task retries until the exact old crop is harvested or cleared.
            for d in range(day,30):
                visits[d].append(Task(point, 'CLEAR_CROP', tile['crop'], tile['planted_day']))
        if item in rules.CROPS:
            cycles = crop_cycles(item, first, point, period=crop_period)
            if not cycles:
                return None
            for index, (generation, end) in enumerate(cycles):
                lot = f'redeploy:{tick}:{point[0]}:{point[1]}:{item}:{generation}'
                spec = dict(point=point, item=item, generation=generation, end=end, lot=lot)
                lots.append(spec)
                crops.append(spec)
                stop = cycles[index+1][0] if index+1 < len(cycles) else 30
                fertilizer_days={d for d,q,f in crop_service(item,generation,end,fertilized) if f}
                for d in range(generation, min(30,stop+int(crop_period is not None and stop==end))):
                    # Once paid, the executor binds this lot to its actual generation.
                    if d + rules.CROPS[item]['first_yield_day'] <= 29:
                        visits[d].append(Task(point, 'PLANT', item, generation, lot))
                    if d in fertilizer_days:
                        visits[d].append(Task(point, 'FERTILIZE', item, generation, lot))
                    visits[d].append(Task(point, 'WATER', item, generation, lot))
                    if d >= end or rules.CROPS[item]['ongoing'] and d-generation >= rules.CROPS[item]['first_yield_day']:
                        visits[d].extend((Task(point, 'HARVEST', item, generation, lot),
                                          Task(nearest_shed(point), 'DEPOSIT', item, None, lot)))
        else:
            if first + rules.ANIMALS[item]['first_yield_day'] > 29:
                return None
            lot = f'redeploy:{tick}:{point[0]}:{point[1]}:{item}:{first}'
            spec = dict(point=point, item=item, generation=first, end=29, lot=lot)
            lots.append(spec)
            herds.append(spec)
            for d in range(first, 30):
                if d + rules.ANIMALS[item]['first_yield_day'] <= 29:
                    visits[d].append(Task(point, 'PLACE', item, first, lot))
                visits[d].append(Task(point, 'HARVEST', item, first, lot))
                if d < 29:
                    visits[d].extend((Task(point, 'FEED', item, first, lot), Task(point, 'CARE', item, first, lot)))
                visits[d].extend((Task(point, 'COLLECT_FERTILIZER', item, first, lot),
                                  Task(nearest_shed(point), 'DEPOSIT', rules.ANIMALS[item]['product'], None, lot),
                                  Task(nearest_shed(point), 'DEPOSIT', 'FERTILIZER', None, lot)))
    work = []
    for d, tasks in visits.items():
        if tasks:
            if crop_period is not None:
                # Finish the same-field harvest/replant handoff before the
                # cargo return, preserving a single material delivery tour.
                deliveries={t.item:t for t in tasks if t.operation=='DEPOSIT'}
                tasks=[t for t in tasks if t.operation!='DEPOSIT']+list(deliveries.values())
            work.append(calendar.insert(days[d], workers, d, tuple(tasks), allow_hire))
    wage = sum(row['marginal_wage'] for row in work)
    suffix = '-redeploy-' + str(tick) + '-' + '-'.join(f'{p[0]}.{p[1]}' for p in points) + '-' + item.lower()
    suffix += '-fertilized' if fertilized else ''
    suffix += '-hire' if allow_hire else '-paid'
    suffix += f'-turnover{crop_period}' if crop_period is not None else ''
    candidate = ProductionProgramme(programme.name+suffix, tuple(days), tuple(workers))
    if not controller.can_continue(candidate):
        raise ValueError('A redeployment changed an executed prefix')
    return Revision(candidate, dict(kind='productive_redeployment',
        mode='same_day_short_crop_cycles' if crop_period is not None else 'occupied_plot_successor',
        crop_period=crop_period,
        item=item, point=points[0], points=points, step=tick, estimate=estimate-wage,fertilized=fertilized,
        marginal_wages=wage, retired_animals=retired, added_plantings=crops, added_herds=herds,
        lots=lots, removed_tasks=removed, cancelled_points=points, work=work,
        price_basis='Biological shortlist only; complete native execution must improve own cash and relative value'))


def redeployment_variants(programme, observation, controller, *, price, day_states=(), limit=4):
    """Expose distinct occupied-site alternatives before the search spends its budget."""
    tick = int(observation['step'])
    if tick % 24 or tick < 216 or tick > 600:
        return ()
    day = tick // 24
    farm = observation['farms'][int(observation['player'])]
    ranked = []
    for y, row in enumerate(farm['tiles']):
        for x, tile in enumerate(row):
            if not isinstance(tile, dict) or not (tile.get('crop') or tile.get('animal')):
                continue
            point = (x, y)
            old_item = tile.get('animal', tile.get('crop'))
            first = release_day(tile, day)
            if tile.get('animal'):
                old = _animal_value(point, old_item, tile['placed_day'], day, price, tile)
            else:
                old = _site_loss(programme, point, tick, 30, observation, price)
            for item in (*rules.CROPS, *rules.ANIMALS):
                if item == old_item:
                    continue
                if item in rules.CROPS:
                    cycles = crop_cycles(item, first, point)
                    if not cycles:
                        continue
                    new = crop_input_value(point,item,cycles,price,False)
                    improved = crop_input_value(point,item,cycles,price,True)
                    if improved>new:
                        ranked.append((improved-old,point,item,old_item,True))
                else:
                    if first + rules.ANIMALS[item]['first_yield_day'] > 29:
                        continue
                    new = _animal_value(point, item, first, first, price)
                ranked.append((new-old, point, item, old_item,False))
    ranked.sort(key=lambda row: (-row[0], row[1], row[2]))
    # Preserve frontier diversity: one cheap local change, a small cohort, and
    # the best different successor or displaced product. Native quotes choose.
    choices, signatures = [], set()
    for gain, point, item, old, fertilized in ranked:
        signature = (item, old)
        if signature in signatures or gain <= 0:
            continue
        signatures.add(signature)
        choices.append((gain, (point,), item,fertilized))
        if len(choices) == 1:
            neighbours = [(g, p) for g, p, successor, source,fert in ranked if g > 0 and successor == item
                          and source == old and fert==fertilized and p != point and distance(point, p) <= 3]
            if neighbours:
                g, p = neighbours[0]
                choices.append((gain+g, (point, p), item,fertilized))
        if len(choices) >= limit:
            break
    inputs=[]
    for estimate,points,item,fertilized in choices[:limit]:
        inputs.append((estimate,points,item,fertilized))
        if fertilized:
            gain=0
            for point in points:
                tile=farm['tiles'][point[1]][point[0]]
                cycles=crop_cycles(item,release_day(tile,day),point)
                gain+=crop_input_value(point,item,cycles,price,True)-crop_input_value(point,item,cycles,price,False)
            inputs.append((estimate-gain,points,item,False))
    result = []
    for estimate, points, item,fertilized in inputs:
        revision = build_revision(programme, observation, controller, points, item,
            day_states=day_states, estimate=estimate,fertilized=fertilized)
        if revision is not None:
            result.append(revision)
            if any(row['estimated_overload']>0 for row in revision.detail['work']):
                hired=build_revision(programme,observation,controller,points,item,
                    day_states=day_states,estimate=estimate,allow_hire=True,fertilized=fertilized)
                if hired.programme.workers!=revision.programme.workers:
                    result.append(hired)
    turnovers=[]
    for estimate,points,item,fertilized in inputs:
        if item not in ('WHEAT','CARROT'):
            continue
        for period in (2,3,4):
            delta=0
            for point in points:
                first=release_day(farm['tiles'][point[1]][point[0]],day)
                old=crop_cycles(item,first,point)
                new=crop_cycles(item,first,point,period=period)
                delta+=crop_input_value(point,item,new,price,fertilized)-crop_input_value(point,item,old,price,fertilized)
            if delta>0:
                turnovers.append((estimate+delta,points,item,fertilized,period))
    if turnovers:
        estimate,points,item,fertilized,period=max(turnovers,key=lambda r:(r[0],r[1],r[2],r[4]))
        revision=build_revision(programme,observation,controller,points,item,day_states=day_states,
            estimate=estimate,fertilized=fertilized,crop_period=period)
        if revision is not None:
            result.append(revision)
            if any(row['estimated_overload']>0 for row in revision.detail['work']):
                hired=build_revision(programme,observation,controller,points,item,day_states=day_states,
                    estimate=estimate,fertilized=fertilized,crop_period=period,allow_hire=True)
                if hired.programme.workers!=revision.programme.workers:
                    result.append(hired)
    if turnovers or any(r.detail['fertilized'] for r in result):
        result.sort(key=lambda r:(-r.detail['estimate'],r.programme.name))
    return tuple(result)
