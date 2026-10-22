"""Upcoming public production phases, without assumed future actions."""
import math

PRODUCTS = ('WHEAT', 'CARROT', 'TOMATO', 'STRAWBERRY', 'MELON', 'EGG', 'MILK', 'WOOL', 'FERTILIZER')
INDEX = {p: i for i, p in enumerate(PRODUCTS)}
ANIMAL_PRODUCT = {'GOOSE': 'EGG', 'COW': 'MILK', 'SHEEP': 'WOOL'}
FIRST = {'WHEAT': 4, 'CARROT': 3, 'MELON': 12, 'TOMATO': 8, 'STRAWBERRY': 10,
         'GOOSE': 4, 'COW': 8, 'SHEEP': 6}
INTERVAL = {'GOOSE': 1, 'COW': 2, 'SHEEP': 3, 'TOMATO': 1, 'STRAWBERRY': 2}


def extra_features(observation, seat):
    day = int(observation['step'])//24
    phase = [0]*36
    bonus = [0]*9
    fertility = [0]*27
    farm = observation['farms'][seat]
    empty = 0
    buildings = 0
    for row in farm['tiles']:
        for tile in row:
            if tile is None:
                empty += 1
                continue
            if not isinstance(tile, dict):
                continue
            animal = tile.get('animal')
            item = ANIMAL_PRODUCT[animal] if animal else tile.get('crop')
            if not item:
                buildings += int(tile.get('kind') in ('COOP', 'PASTURE'))
                continue
            p = INDEX[item]
            origin = tile['placed_day' if animal else 'planted_day']
            key = animal or item
            for offset in range(1, 5):
                elapsed = day+offset-origin-FIRST[key]
                if key in INTERVAL:
                    due = elapsed >= 0 and elapsed % INTERVAL[key] == 0
                    if not animal:
                        due = due and elapsed//INTERVAL[key] < 4
                else:
                    due = elapsed == 0
                phase[(offset-1)*9+p] += int(due)
            if animal:
                bonus[p] += tile.get('pending_care_bonus', 0)
            else:
                for offset in range(3):
                    fertility[offset*9+p] += int(tile.get('fertilized_until_day', -1) >= day+offset)
    competitor = [0]*9
    for row in observation['farms'][1-seat]['tiles']:
        for tile in row:
            if isinstance(tile, dict):
                item = ANIMAL_PRODUCT[tile['animal']] if tile.get('animal') else tile.get('crop')
                if item:
                    competitor[INDEX[item]] += 1
    return dict(phase=phase, care_bonus=bonus, fertility=fertility, competitor=competitor,
                liquidity=[math.log1p(max(0, float(farm['money']))/1000)],
                space=[len(farm['unlocked_quadrants']), empty/10, buildings/4])
