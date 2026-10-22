"""Allocate warehouse space to goods with valuable declared waiting windows."""
from collections import Counter
import heapq
import json
from functools import lru_cache

from . import rules
from .compiler import distance, nearest_shed
from .sale_ledger import PriceLadder
from .native_sale import waiting_values
from .biological_cache import deliveries as cached_deliveries
from .native_sale import _BACKEND
from .native_settlement import sale_caps as native_caps, sale_caps_with_prediction
from . import PLANNER_BACKEND
from .p110_sale import active_prediction, overlay_scenarios


@lru_cache(maxsize=4)
def _price_book(parameters):
    params = json.loads(parameters)
    return {item:PriceLadder(lambda inventory,p=item:rules.market_price(p,inventory,params),
                            (params or rules.MARKET_PARAMS)[item]['I0']) for item in rules.PRODUCTS}


def _quotes(market):
    params = market.get('params')
    book = _price_book(json.dumps(params, sort_keys=True, separators=(',', ':')))
    return book


_SHED_DISTANCES = tuple(tuple(min(abs(x-a)+abs(y-b) for a,b in ((4,4),(4,5),(5,4),(5,5)))
    for x in range(10)) for y in range(10))


def _deliveries(farm, start, end, products=None):
    return cached_deliveries(farm, start, end, products, _SHED_DISTANCES)


def _reference_deliveries(farm, start, end, products=None):
    """Existing assets continue receiving service; no future investment is assumed."""
    events = []
    # Rule-owned tiles contain scalar fields. Copy each tile once; no recursive
    # copy of the farm, controller, inventories or programme is needed here.
    def copy_relevant(tile):
        if not isinstance(tile,dict):
            return tile
        item = rules.ANIMALS[tile['animal']]['product'] if tile.get('animal') else tile.get('crop')
        return dict(tile) if products is None or item in products else None
    tiles = [[copy_relevant(tile) for tile in row] for row in farm['tiles']]
    shadow = dict(farm, tiles=tiles)
    for day in range(start // 24, end // 24 + 1):
        for y, row in enumerate(tiles):
            for x, tile in enumerate(row):
                if not isinstance(tile, dict):
                    continue
                item = rules.ANIMALS[tile['animal']]['product'] if tile.get('animal') else tile.get('crop')
                if not item or item in ('WHEAT', 'FERTILIZER'):
                    continue
                ready = bool(tile.get('animal')) or day - tile['planted_day'] >= rules.CROPS[item]['first_yield_day']
                if ready and tile.get('yield_units', 0):
                    earliest = max(start, day * 24) + _SHED_DISTANCES[y][x] + 2
                    events.append((earliest, item, tile['yield_units']))
                    tile['yield_units'] = 0
                    if item in rules.CROPS and not rules.CROPS[item]['ongoing']:
                        tiles[y][x] = None
                        continue
                if tile.get('animal'):
                    tile['fed_today'], tile['cared_today'] = True, True
                elif not tile.get('watered_today'):
                    tile['watered_today'] = True
                    data = rules.CROPS[item]
                    if not data['ongoing'] and (data['max_yield_day'] + 1) // 2 <= day - tile['planted_day'] <= data['max_yield_day']:
                        tile['yield_units'] = min(data['max_yield'], tile['yield_units'] + (2 if tile['fertilized_until_day'] >= day else 1))
        rules._daily_refresh_plants(shadow, day, 24)
        rules._daily_refresh_animals(shadow, day)
    return events


def _sale_scenarios(observation, farm, private, end, products=None):
    start = int(observation['step'])
    own = _deliveries(farm, start, end, products)
    other = _deliveries(observation['farms'][1-int(observation['player'])], start, end, products)
    for bag in private['inventories']:
        for item, qty in bag.items():
            if item in rules.PRODUCTS and item not in ('WHEAT', 'FERTILIZER'):
                own.append((min(end, (start // 24 + 1) * 24), item, qty))
    consumption = Counter()
    for shop in observation['town']['unlocked_shops']:
        products = rules.SHOPS[shop]
        for item in products:
            consumption[item] += 2 if len(products) == 1 else 1
    consumption.update(observation['town'].get('_forecast_shop_consumption',{}))
    own_arrivals = {}
    for earliest, item, qty in own:
        tick = earliest + 4
        amounts = own_arrivals.get(tick)
        if amounts is None:
            amounts = own_arrivals[tick] = {}
        amounts[item] = amounts.get(item, 0) + qty
    demand_schedule = {}
    for tick in range(start + (-start) % 4, end + 1, 4):
        demand = dict(consumption)
        if tick % 24 == 0:
            for item in rules.TOWN_CENTER_PRODUCTS:
                demand[item] = demand.get(item, 0) + 1
        if demand:
            demand_schedule[tick] = demand
    scenarios = []
    # Consumption follows the market at ticks divisible by four. Its first
    # usable quote is the following tick, regardless of the current phase.
    windows = set(range(start + 1 + (-start) % 4, end + 1, 4)) | {end}
    # The two arrival delays are hypotheses about transport and sale timing.
    # Existing own output is included as supply, so a full barn is not valued
    # as if all the remaining market demand belonged to the stored batch.
    for index,delay in enumerate((4, 12)):
        rival_arrivals = {}
        for earliest, item, qty in other:
            tick = earliest + delay
            amounts = rival_arrivals.get(tick)
            if amounts is None:
                amounts = rival_arrivals[tick] = {}
            amounts[item] = amounts.get(item, 0) + qty
        cargo=observation.get('_public_rival_cargo')
        if cargo and start<=cargo['origin']+delay<=end:
            quantities=Counter(cargo['quantities'])
            if index:
                quantities.update(cargo.get('uncertain_upper',{}))
            if quantities:
                tick = cargo['origin'] + delay
                amounts = rival_arrivals.setdefault(tick, {})
                for item,qty in quantities.items():
                    amounts[item] = amounts.get(item, 0) + qty
        # Keep an imminent pre-arrival alternative. Arrival times remain
        # transport hypotheses; both scenarios must support any decision.
        imminent = [tick for tick in rival_arrivals if start+1 < tick <= min(end, start+24)]
        if imminent:
            windows.add(min(imminent)-1)
        scenarios.append((own_arrivals, rival_arrivals))
    result = []
    shared_ticks = windows | demand_schedule.keys() | own_arrivals.keys()
    for own_arrivals, rival_arrivals in scenarios:
        events = []
        for tick in sorted(shared_ticks | rival_arrivals.keys()):
            if start <= tick <= end:
                events.append((tick, own_arrivals.get(tick, {}), rival_arrivals.get(tick, {}),
                               demand_schedule.get(tick, {})))
        result.append(events)
    prediction=active_prediction(observation)
    if prediction is not None:
        return overlay_scenarios(observation,result,windows,prediction,end)
    return result, sorted(windows)


def _reference_sale_caps(observation, farm, private, market, reserves, cash_required, *, incoming_purchase_units=0):
    """Return liquidations after funding and physically occupied space reservations.

    This is a receding comparison of selling now with keeping a tail of the
    current batch until one future window. Consumption already declared in the
    observation is used. Future service and sale batches remain hypotheses.
    """
    amounts = {p:max(0, private['shed'].get(p, 0)-reserves.get(p, 0)) for p in rules.PRODUCTS}
    start = int(observation['step'])
    end = min(718, start + 144)
    eligible = {p:q for p,q in amounts.items() if q and p not in ('WHEAT','FERTILIZER')}
    bag_units = sum(sum(bag.values()) for bag in private['inventories'])
    reserved_units = sum(reserves.values())
    unpriced_units = sum(q for p,q in private['shed'].items() if p not in rules.PRODUCTS)
    capacity = max(0, 100-bag_units-reserved_units-unpriced_units-incoming_purchase_units)
    if not eligible or end <= start or capacity == 0:
        return amounts, None
    prices = {}
    book = _quotes(market)
    available_cash = farm['money']
    for item, qty in amounts.items():
        inventory = market['inventory'][item]
        ladder = []
        for _ in range(qty):
            price = book[item].quote(inventory)
            ladder.append(price)
            inventory += price > 1
        prices[item] = ladder
        available_cash += sum(ladder)
    if available_cash <= cash_required:
        return amounts, None
    scenarios, windows = _sale_scenarios(observation, farm, private, end, eligible)
    offers, schedules, gains = [], {}, {}
    for item, qty in eligible.items():
        max_hold = min(qty, capacity)
        events = [[(tick,own.get(item,0),rival.get(item,0),demand.get(item,0))
                   for tick,own,rival,demand in scenario] for scenario in scenarios]
        params = (market.get('params') or rules.MARKET_PARAMS)[item]
        gain_by_hold, time_by_hold, own_gains, rival_gains, batch_gains, other_own_gains = waiting_values(
            book[item],params,market['inventory'][item],qty,max_hold,events,windows,start)
        gains[item], schedules[item] = gain_by_hold, time_by_hold
        schedules[(item,'own')],schedules[(item,'rival')] = own_gains,rival_gains
        schedules[(item,'batch')],schedules[(item,'own_other')] = batch_gains,other_own_gains
        if len(gain_by_hold)>1 and gain_by_hold[1]>0:
            heapq.heappush(offers, (-gain_by_hold[1], item, 1))
    held, records = Counter(), []
    while offers and sum(held.values()) < capacity:
        negative_gain, item, count = heapq.heappop(offers)
        opportunity_cost = prices[item][-count]
        if available_cash-opportunity_cost < cash_required:
            continue
        available_cash -= opportunity_cost
        held[item] = count
        amounts[item] -= 1
        next_count = count+1
        if next_count < len(gains[item]):
            increment = gains[item][next_count]-gains[item][count]
            if increment > 0:
                heapq.heappush(offers, (-increment, item, next_count))
    for item, count in held.items():
        records.append(dict(item=item,quantity=count,declared_gain=gains[item][count],
                            own_gain=schedules[(item,'own')][count],
                            held_batch_gain=schedules[(item,'batch')][count],
                            own_other_batch_premium=schedules[(item,'own_other')][count],
                            rival_premium=schedules[(item,'rival')][count],window=schedules[item][count]))
    return amounts, dict(step=start,held=records,space_limit=capacity,cash_floor=cash_required,
                         projected_cash_after_sales=available_cash) if records else None


def sale_caps(observation, farm, private, market, reserves, cash_required, *, incoming_purchase_units=0):
    prediction=active_prediction(observation)
    if _BACKEND=='rust' and PLANNER_BACKEND=='compiled' and prediction is not None:
        return sale_caps_with_prediction(observation,farm,private,market,reserves,cash_required,prediction,
            incoming_purchase_units=incoming_purchase_units)
    evaluator = native_caps if _BACKEND == 'rust' and prediction is None else _reference_sale_caps
    return evaluator(observation, farm, private, market, reserves, cash_required,
                     incoming_purchase_units=incoming_purchase_units)
