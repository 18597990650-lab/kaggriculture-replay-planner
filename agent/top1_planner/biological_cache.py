"""Exact, bounded biological forecasts reusable independently of market state."""
from functools import lru_cache
import time

from . import rules

_WARMED = set()
_STATS = dict(warmed=0, consumed=0)


def signature(tile, day, days):
    if tile.get('animal'):
        return ('animal', tile['animal'], day-tile['placed_day'], tile.get('yield_units', 0),
                tile.get('pending_care_bonus', 0), 0, False, days)
    item = tile['crop']
    return ('crop', item, day-tile['planted_day'], tile.get('yield_units', 0), 0,
            max(-1, tile.get('fertilized_until_day', -1)-day),
            bool(tile.get('watered_today')) if not rules.CROPS[item]['ongoing'] else False, days)


@lru_cache(maxsize=32768)
def forecast(key):
    kind, item, age, held, bonus, fertilized, watered, days = key
    if kind == 'animal':
        tile = rules._new_animal(item, -age)
        tile['pending_care_bonus'] = bonus
    else:
        tile = rules._new_plant(item, -age, 24)
        tile['fertilized_until_day'] = fertilized
        tile['watered_today'] = watered
    tile['yield_units'] = held
    farm = dict(tiles=[[tile]])
    events = []
    for offset in range(days):
        tile = farm['tiles'][0][0]
        if tile is None:
            break
        ready = kind == 'animal' or offset+age >= rules.CROPS[item]['first_yield_day']
        if ready and tile.get('yield_units', 0):
            events.append((offset, tile['yield_units']))
            tile['yield_units'] = 0
            if kind == 'crop' and not rules.CROPS[item]['ongoing']:
                break
        if kind == 'animal':
            tile['fed_today'], tile['cared_today'] = True, True
        elif not tile['watered_today']:
            tile['watered_today'] = True
            data = rules.CROPS[item]
            if not data['ongoing'] and (data['max_yield_day']+1)//2 <= offset+age <= data['max_yield_day']:
                tile['yield_units'] = min(data['max_yield'], tile['yield_units']+(2 if fertilized >= offset else 1))
        rules._daily_refresh_plants(farm, offset, 24)
        rules._daily_refresh_animals(farm, offset)
    return tuple(events)


def deliveries(farm, start, end, products, distances):
    day, final_day = start//24, end//24
    events = []
    for y, row in enumerate(farm['tiles']):
        for x, tile in enumerate(row):
            if not isinstance(tile, dict):
                continue
            item = rules.ANIMALS[tile['animal']]['product'] if tile.get('animal') else tile.get('crop')
            if not item or item in ('WHEAT', 'FERTILIZER') or products is not None and item not in products:
                continue
            key = signature(tile, day, final_day-day+1)
            prepared = key in _WARMED
            misses = forecast.cache_info().misses if prepared else 0
            values = forecast(key)
            if prepared:
                _WARMED.remove(key)
                _STATS['consumed'] += forecast.cache_info().misses == misses
            for offset, quantity in values:
                arrival = max(start, (day+offset)*24)+distances[y][x]+2
                events.append((offset, y, x, arrival, item, quantity))
    events.sort(key=lambda row: row[:3])
    return [(arrival, item, quantity) for _, _, _, arrival, item, quantity in events]


def forecast_jobs(observation):
    """Enumerate state keys, never predicted actions, cash, inventory or shops."""
    day = int(observation['step'])//24
    next_day = ((day//3)+1)*3
    assets = set()
    for farm in observation['farms']:
        for row in farm['tiles']:
            for tile in row:
                if not isinstance(tile, dict):
                    continue
                if tile.get('animal'):
                    assets.add(('animal', tile['animal'], tile['placed_day']))
                elif tile.get('crop') and tile['crop'] != 'WHEAT':
                    assets.add(('crop', tile['crop'], tile['planted_day']))
    for future_day in range(next_day, 30):
        days = min(7, 30-future_day)
        for kind, item, generation in sorted(assets):
            age = future_day-generation
            data = rules.ANIMALS[item] if kind == 'animal' else rules.CROPS[item]
            if kind == 'crop' and age > data['first_yield_day']+(data['max_yield']-1)*data['interval'] and data['ongoing']:
                continue
            limit = data['max_held'] if kind == 'animal' else data['max_yield']
            for held in range(limit+1):
                bonuses = range(data['first_yield_day']+1) if kind == 'animal' else (0,)
                for bonus in bonuses:
                    for fert in ((0,) if kind == 'animal' else (-1, 0, 1, 2)):
                        for watered in ((False, True) if kind == 'crop' and not data['ongoing'] else (False,)):
                            yield kind, item, age, held, bonus, fert, watered, days


class ForecastPreparation:
    def __init__(self):
        self.node = None
        self.jobs = None

    def advance(self, observation, deadline):
        tick = int(observation['step'])
        node = (tick//72+1)*72
        if tick < 120 or node > 576 or tick % 24 == 0:
            return None
        if self.node != node:
            self.node = node
            self.jobs = iter(forecast_jobs(observation))
        if self.jobs is None or time.perf_counter() >= deadline:
            return None
        begin = time.perf_counter()
        before = forecast.cache_info()
        attempted = 0
        while time.perf_counter() < deadline-.002:
            key = next(self.jobs, None)
            if key is None:
                self.jobs = None
                break
            prior = forecast.cache_info().misses
            forecast(key)
            if forecast.cache_info().misses != prior:
                if len(_WARMED) >= 32768:
                    _WARMED.clear()
                _WARMED.add(key)
                _STATS['warmed'] += 1
            attempted += 1
        after = forecast.cache_info()
        return dict(kind='FREE_STEP_FORECAST_PREPARATION', step=tick, target_step=node,
            seconds=time.perf_counter()-begin, evaluated=attempted,
            new_entries=after.misses-before.misses, cached_entries=after.currsize,
            warmed_total=_STATS['warmed'], consumed_total=_STATS['consumed'],
            completed=self.jobs is None)
