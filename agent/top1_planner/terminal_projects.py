"""Reassign the final day's visible harvest and price the complete delivery route."""
import time

from . import rules
from .compiler import distance, nearest_shed
from .programmes import ProductionProgramme, Task
from . import quote_timing


def harvest_tasks(observation):
    farm = observation['farms'][int(observation['player'])]
    tasks, totals = [], {}
    for y, row in enumerate(farm['tiles']):
        for x, tile in enumerate(row):
            if not isinstance(tile, dict):
                continue
            item = tile.get('animal', tile.get('crop'))
            if item not in {*rules.CROPS, *rules.ANIMALS}:
                continue
            born = tile.get('placed_day', tile.get('planted_day'))
            point, ops = (x, y), []
            quantity = tile.get('yield_units', 0)
            if item in rules.CROPS:
                crop = rules.CROPS[item]
                age = 29-born
                if age < crop['first_yield_day']:
                    continue
                if (not crop['ongoing'] and not tile['watered_today']
                        and (crop['max_yield_day']+1)//2 <= age <= crop['max_yield_day']
                        and quantity < crop['max_yield']):
                    ops.append(Task(point, 'WATER', item, born))
                    quantity = min(crop['max_yield'], quantity+(2 if tile.get('fertilized_until_day', -1) >= 29 else 1))
                product = item
            else:
                product = rules.ANIMALS[item]['product']
                if tile['fertilizer_available']:
                    tasks.append(dict(point=point, ops=(Task(point, 'COLLECT_FERTILIZER', item, born),),
                                      product='FERTILIZER', quantity=1))
                    totals['FERTILIZER'] = totals.get('FERTILIZER', 0)+1
            if quantity:
                tasks.append(dict(point=point, ops=tuple(ops)+(Task(point, 'HARVEST', item, born),),
                                  product=product, quantity=quantity))
                totals[product] = totals.get(product, 0)+quantity
    prices = {}
    for product, quantity in totals.items():
        inventory = observation['market']['inventory'][product]
        value = 0
        for _ in range(quantity):
            price = rules.market_price(product, inventory, observation['market'].get('params'))
            value += price
            inventory += price > 1
        prices[product] = value/quantity
    for row in tasks:
        row['value'] = row['quantity']*prices[row['product']]
    return tasks


def _pack(programme, observation, tasks, count):
    farm = observation['farms'][int(observation['player'])]
    shadow = dict(farm, hands=[list(p) for p in farm['hands']])
    while len(shadow['hands']) < count:
        shadow['hands'].append(rules._spawn_hand(shadow, 10))
    starts = [tuple(shadow['farmer']), *map(tuple, shadow['hands'])]
    routes, costs, used = [[] for _ in starts], [0 for _ in starts], set()
    while True:
        best = None
        for k, task in enumerate(tasks):
            if k in used:
                continue
            for actor, route in enumerate(routes):
                for index in range(len(route)+1):
                    before = tasks[route[index-1]]['point'] if index else starts[actor]
                    after = tasks[route[index]]['point'] if index < len(route) else None
                    old = distance(before, after) if after is not None else distance(before, nearest_shed(before))
                    extra = distance(before, task['point'])+len(task['ops'])-old
                    extra += distance(task['point'], after if after is not None else nearest_shed(task['point']))
                    if costs[actor]+extra+2 > 20:
                        continue
                    score = task['value']/max(1, extra), task['value'], -extra, -actor, -index, -k
                    if best is None or score > best[0]:
                        best = score, actor, index, k, extra
        if best is None:
            break
        _, actor, index, k, extra = best
        routes[actor].insert(index, k)
        costs[actor] += extra
        used.add(k)
    rows = {}
    for actor, route in enumerate(routes):
        sequence = tuple(t for k in route for t in tasks[k]['ops'])
        if sequence:
            sequence += (Task(nearest_shed(sequence[-1].point), 'DROP', None, None),)
        rows[actor] = sequence
    days, workers = list(programme.days), list(programme.workers)
    days[29], workers[29] = rows, count
    candidate = ProductionProgramme(programme.name+f'-terminal-{count}', tuple(days), tuple(workers))
    wage = sum(rules._hire_cost(n, 1) for n in range(farm['hires_today'], count))
    return candidate, dict(hands=count, tasks=len(used), gross=sum(tasks[k]['value'] for k in used),
                           wage_screen=wage, durations=costs)


def terminal_variants(programme, observation):
    if int(observation['step']) != 696:
        return ()
    farm = observation['farms'][int(observation['player'])]
    tasks = harvest_tasks(observation)
    counts = sorted({max(len(farm['hands']), n) for n in (0, 4, 8, 12, programme.workers[29])})
    return tuple(sorted((_pack(programme, observation, tasks, n) for n in counts),
                        key=lambda r: (-(r[1]['gross']-r[1]['wage_screen']), r[1]['hands'])))


def close_day(observation, configuration, controller, world, seconds):
    from .recipe import compile_recipe
    from .available_crop_admission import quote_summary
    begin = time.perf_counter()
    deadline = begin+seconds
    record = dict(kind='TERMINAL_HARVEST_REALLOCATION', step=696, changed=False, quotes=[], proposals=[])
    if seconds < .35:
        return controller, dict(record, seconds=0, reason='No complete final-day comparison fits')
    def quote(programme):
        start=time.perf_counter()
        result=compile_recipe(observation, configuration, programme, world,
            continuation=controller.fork(programme=programme), settlement=True,
            bind_herd=controller.bind_herd, adaptive_crops=controller.adaptive_crops)
        quote_timing.observe(observation['step'],True,time.perf_counter()-start)
        return result
    baseline = quote(controller.programme).result
    cost = quote_timing.estimate(observation['step'],True)
    control = quote_summary(baseline,observation)
    winner, score = None, control['relative']
    variants = terminal_variants(controller.programme, observation)
    record['proposals'] = [meta for p, meta in variants]
    for programme, meta in variants:
        if time.perf_counter()+cost*1.25+.05 > deadline:
            break
        before = time.perf_counter()
        result = quote(programme).result
        cost = quote_timing.estimate(observation['step'],True)
        value = quote_summary(result,observation)
        relative = value['relative']
        passed = (not result['issues'] and value['own'] >= control['own'] and relative > score
                  and time.perf_counter() <= deadline)
        record['quotes'].append(dict(**meta, own_delta=value['own']-control['own'],
            relative_delta=relative-control['relative'],
            harvested=result['harvest'], issues=result['issues'], admitted=passed))
        if passed:
            winner, score = programme, relative
    if winner is not None:
        controller = controller.fork(programme=winner)
        record.update(changed=True, selected=winner.name)
    return controller, dict(record, seconds=time.perf_counter()-begin)
