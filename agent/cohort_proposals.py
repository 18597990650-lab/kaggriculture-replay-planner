"""Current paid-crop frontier; no historical episode lookups."""
from itertools import combinations, permutations

def proposals(controller, obs, baseline=None, *, price=None):
    from top1_planner import rules
    from top1_planner.compiler import distance, nearest_shed
    from top1_planner.redeployment import _animal_value
    from top1_planner.task_allocation import QuotePrices, _site_loss
    tick, farm = obs['step'], obs['farms'][obs['player']]
    day = tick // 24
    if price is None:
        if baseline is None:
            raise ValueError('A completed incumbent quote or explicit price path is required')
        price = QuotePrices(obs, baseline)
    plots = []
    for y, row in enumerate(farm['tiles']):
        for x, tile in enumerate(row):
            if not isinstance(tile, dict) or tile.get('crop') not in ('WHEAT','CARROT','MELON'):
                continue
            p = (x, y)
            item = tile['crop']
            old = _site_loss(controller.programme, p, tick, 30, obs, price)
            salvaged = (tile['yield_units'] if day - tile['planted_day'] >= rules.CROPS[item]['first_yield_day'] else 0)
            salvage = price(item, tick + distance(p, nearest_shed(p)) + 4, salvaged)
            new = _animal_value(p, 'SHEEP', day, day, price)
            plots.append(dict(point=p, source=item, gain=new + salvage - old,
                old_remaining_value=old, salvaged_units=salvaged, salvage_value=salvage, new_value=new))
    plots.sort(key=lambda r: (-r['gain'], r['point']))
    options = []
    # A small local group lets one route carry supplies and return all cargo.
    # Each region gets its own frontier so a coarse tie cannot hide a region.
    for region in ('all','west','east','north','south'):
        pool = [r for r in plots if (region == 'all' or
            region == 'west' and r['point'][0] < 5 or region == 'east' and r['point'][0] >= 5 or
            region == 'north' and r['point'][1] < 5 or region == 'south' and r['point'][1] >= 5)][:8]
        for chosen in combinations(pool, 3):
            points = tuple(r['point'] for r in chosen)
            if max(distance(a,b) for a,b in combinations(points,2)) > 4:
                continue
            def length(route):
                return distance(nearest_shed(route[0]), route[0]) + sum(distance(a,b) for a,b in zip(route, route[1:])) + distance(route[-1],nearest_shed(route[-1]))
            route = min(permutations(points), key=lambda r:(length(r),r))
            options.append(dict(region=region, points=route, coarse_gain=sum(r['gain'] for r in chosen), travel=length(route)))
    options.sort(key=lambda r:(-r['coarse_gain'],r['travel'],r['points']))
    selected, seen, regions = [], set(), set()
    for item in options:
        identity = tuple(sorted(item['points']))
        if identity in seen or item['region'] in regions:
            continue
        selected.append(item)
        seen.add(identity)
        regions.add(item['region'])
    return plots, selected[:4]
