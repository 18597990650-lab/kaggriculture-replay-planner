"""Public-only daily descriptors shared by extraction and runtime retrieval."""
PRODUCTS = ('WHEAT', 'CARROT', 'TOMATO', 'STRAWBERRY', 'MELON', 'EGG', 'MILK', 'WOOL', 'FERTILIZER')
SHOPS = ('BAKERY', 'PIZZA_SHOP', 'BRUNCH_SPOT', 'YARN_STORE', 'ICE_CREAM_SHOP', 'PET_CAFE', 'SMOOTHIE_SHOP', 'FARMERS_MARKET')
ANIMAL_PRODUCT = {'GOOSE': 'EGG', 'COW': 'MILK', 'SHEEP': 'WOOL'}
FIRST_YIELD = {'WHEAT': 2, 'CARROT': 2, 'TOMATO': 8, 'STRAWBERRY': 10, 'MELON': 10,
               'GOOSE': 4, 'COW': 8, 'SHEEP': 6}
PRODUCT_INDEX = {p: i for i, p in enumerate(PRODUCTS)}


def snapshot(observation, seat):
    day = int(observation['step']) // 24
    farm = observation['farms'][seat]
    assets = [0] * 9
    quadrants = [0] * 36
    maturity = [0] * 36
    ready = [0] * 9
    fertilizer = [0] * 9
    stress = [0] * 9
    structures = [0] * 4
    for y, row in enumerate(farm['tiles']):
        for x, tile in enumerate(row):
            if not isinstance(tile, dict):
                continue
            item = ANIMAL_PRODUCT[tile['animal']] if tile.get('animal') else tile.get('crop')
            quadrant = (2 if y >= 5 else 0) + (1 if x >= 5 else 0)
            if not item:
                if tile.get('kind') in ('COOP', 'PASTURE'):
                    structures[quadrant] += 1
                continue
            p = PRODUCT_INDEX[item]
            assets[p] += 1
            quadrants[quadrant * 9 + p] += 1
            animal = tile.get('animal')
            age = day - tile['placed_day' if animal else 'planted_day']
            remaining = FIRST_YIELD[animal or item] - age
            bucket = 0 if remaining > 3 else 1 if remaining > 0 else 2 if age < FIRST_YIELD[animal or item] + 4 else 3
            maturity[bucket * 9 + p] += 1
            ready[p] += tile.get('yield_units', 0)
            fertilizer[p] += int(tile.get('fertilizer_available', False) if animal else tile.get('fertilized_until_day', -1) >= day)
            stress[p] += min(3, tile.get('consecutive_unfed' if animal else 'consecutive_unwatered', 0))
    shops = observation['town']['unlocked_shops']
    return dict(assets=assets, shops=[shops.count(s) for s in SHOPS], quadrants=quadrants,
                maturity=maturity, ready=ready, fertilizer=fertilizer, stress=stress,
                structures=structures, prices=[observation['market']['prices'][p] for p in PRODUCTS],
                cash=float(farm['money']), land=len(farm['unlocked_quadrants']))


def add_history(daily, day):
    current = dict(daily[day])
    current['history'] = [v for lag in (1, 3, 6) for v in daily[max(0, day-lag)]['assets']]
    return current
