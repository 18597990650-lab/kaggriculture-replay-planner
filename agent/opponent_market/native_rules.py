"""Official rule helpers for current-state projection and terminal allocation; Apache-2.0.
Source SHA256: bc8a54879ef02c7ea64b8b333d6a976f0ea65c4949149d01f463f23bccee653e
Functions and constants are verbatim from the installed competition interpreter.
No game runner, initialization side effects, renderer, or external imports.
"""
import math

CROPS = {
    "WHEAT":      {"seed": 10, "first_yield_day": 2, "max_yield_day": 4, "interval": 0, "max_yield": 6, "ongoing": False},
    "CARROT":     {"seed": 20, "first_yield_day": 2, "max_yield_day": 3, "interval": 0, "max_yield": 4, "ongoing": False},
    "TOMATO":     {"seed": 50, "first_yield_day": 8, "max_yield_day": 8, "interval": 1, "max_yield": 4, "ongoing": True},
    "STRAWBERRY": {"seed": 100, "first_yield_day": 10, "max_yield_day": 10, "interval": 2, "max_yield": 4, "ongoing": True},
    "MELON":      {"seed": 80, "first_yield_day": 10, "max_yield_day": 12, "interval": 0, "max_yield": 6, "ongoing": False},
}

ANIMALS = {
    "GOOSE": {"cost": 300, "structure": "COOP",    "first_yield_day": 4, "interval": 1, "max_held": 4, "product": "EGG"},
    "COW":   {"cost": 400, "structure": "PASTURE", "first_yield_day": 8, "interval": 2, "max_held": 6, "product": "MILK"},
    "SHEEP": {"cost": 500, "structure": "PASTURE", "first_yield_day": 6, "interval": 3, "max_held": 6, "product": "WOOL"},
}

PRODUCTS = ["WHEAT", "CARROT", "TOMATO", "STRAWBERRY", "MELON", "EGG", "MILK", "WOOL", "FERTILIZER"]

MARKET_I0 = 10000

PRICE_FLOOR = 1

MARKET_PARAMS = {
    "WHEAT":      {"base":  25, "I0": MARKET_I0, "T": 400, "below_func": "sqrt",   "below_target": 0.80, "above_func": "log",    "above_target": 0.20},
    "CARROT":     {"base":  35, "I0": MARKET_I0, "T": 450, "below_func": "hinge",  "below_target": 1.00, "above_func": "sqrt",   "above_target": 0.70},
    "TOMATO":     {"base":  60, "I0": MARKET_I0, "T": 200, "below_func": "hinge",  "below_target": 0.40, "above_func": "sqrt",   "above_target": 0.60},
    "STRAWBERRY": {"base": 120, "I0": MARKET_I0, "T": 100, "below_func": "sqrt",   "below_target": 0.70, "above_func": "linear", "above_target": 1.60},
    "MELON":      {"base": 250, "I0": MARKET_I0, "T": 300, "below_func": "log",    "below_target": 0.20, "above_func": "sq",     "above_target": 3.60},
    "EGG":        {"base":  50, "I0": MARKET_I0, "T": 332, "below_func": "hinge",  "below_target": 0.40, "above_func": "log",    "above_target": 0.20},
    "MILK":       {"base": 160, "I0": MARKET_I0, "T": 122, "below_func": "sqrt",   "below_target": 0.60, "above_func": "linear", "above_target": 1.60},
    "WOOL":       {"base": 200, "I0": MARKET_I0, "T": 105, "below_func": "log",    "below_target": 0.20, "above_func": "sq",     "above_target": 3.20},
    "FERTILIZER": {"base": 100, "I0": MARKET_I0, "T": 200, "below_func": "linear", "below_target": 0.40, "above_func": "linear", "above_target": 0.40},
}

HINGE_GAIN = 8.0

def _shape(func, x, T=None):
    x = max(0.0, x)
    if func == "linear": return x
    if func == "sq":     return x * x
    if func == "sqrt":   return math.sqrt(x)
    if func == "log":    return math.log(1.0 + x)
    if func == "log10":  return math.log10(1.0 + x)
    if func == "hinge":
        # Degenerates to linear if T is missing or non-positive.
        if not T or T <= 0:
            return x
        u = x / T
        return u + HINGE_GAIN * max(0.0, u - 1.0) ** 2
    return x

FARMER_MOVES = {
    "NORTH": (0, -1),
    "SOUTH": (0, 1),
    "EAST":  (1, 0),
    "WEST":  (-1, 0),
}

LAND_ORDER = ["NE", "SW", "SE"]

LAND_PRICES = [1000, 2000, 4000]

FARM_HAND_COST_MULT = 1

SHOPS = {
    "BAKERY":         ["EGG", "WHEAT"],
    "PIZZA_SHOP":     ["MILK", "TOMATO", "WHEAT"],
    "BRUNCH_SPOT":    ["EGG", "WHEAT", "STRAWBERRY"],
    "YARN_STORE":     ["WOOL"],
    "ICE_CREAM_SHOP": ["STRAWBERRY", "MILK", "WHEAT"],
    "PET_CAFE":       ["CARROT"],
    "SMOOTHIE_SHOP":  ["STRAWBERRY", "MILK"],
    "FARMERS_MARKET": ["WHEAT", "CARROT", "TOMATO", "STRAWBERRY"],
}

TOWN_CENTER_PRODUCTS = [p for p in PRODUCTS if p != "FERTILIZER"]

def get(d, key, default):
    if isinstance(d, dict):
        return d.get(key, default)
    return getattr(d, key, default)

def _quadrant_of(x, y, board_size):
    half = board_size // 2
    return ("N" if y < half else "S") + ("W" if x < half else "E")

def _shed_access_tiles(board_size):
    """Four inner-corner tiles around the shed, in NWSE order."""
    half = board_size // 2
    return [(half - 1, half - 1), (half, half - 1), (half - 1, half), (half, half)]

def _is_shed_adjacent(pos, board_size):
    return tuple(pos) in {(x, y) for (x, y) in _shed_access_tiles(board_size)}

def _new_farm(board_size, starting_money):
    return {
        "money": float(starting_money),
        # tiles[y][x] = None (empty unlocked) | "LOCKED" | dict structure
        "tiles": [
            [_initial_tile(x, y, board_size) for x in range(board_size)]
            for y in range(board_size)
        ],
        "farmer": list(_default_spawn(board_size)),
        "hands": [],
        "unlocked_quadrants": ["NW"],
        "hires_today": 0,
    }

def _initial_tile(x, y, board_size):
    return None if _quadrant_of(x, y, board_size) == "NW" else "LOCKED"

def _default_spawn(board_size):
    """First free shed-access tile, NWSE preference."""
    for tile in _shed_access_tiles(board_size):
        if _quadrant_of(tile[0], tile[1], board_size) == "NW":
            return tile
    return (0, 0)

def _new_private():
    return {
        "shed": {item: 0 for item in PRODUCTS + list(ANIMALS)},
        "seeds": {crop: 0 for crop in CROPS},
        # inventories[0] = main farmer; hands appended/removed each day.
        "inventories": [{}],
    }

def _new_market(params=None):
    params = params or MARKET_PARAMS
    inv = {item: params[item]["I0"] for item in PRODUCTS}
    prices = {item: params[item]["base"] for item in PRODUCTS}
    market = {"inventory": inv, "prices": prices}
    if params is not MARKET_PARAMS:
        market["params"] = params
    return market

def _new_town():
    return {"unlocked_shops": []}

def market_price(item, inventory, params=None):
    """Floor at PRICE_FLOOR."""
    p = (params or MARKET_PARAMS)[item]
    base = p["base"]
    I0 = p["I0"]
    T = p["T"]
    if inventory < I0:
        f = p["below_func"]
        amp = p["below_target"] * base / _shape(f, T, T)
        price = base + amp * _shape(f, I0 - inventory, T)
    else:
        f = p["above_func"]
        amp = p["above_target"] * base / _shape(f, T, T)
        price = base - amp * _shape(f, inventory - I0, T)
    return max(PRICE_FLOOR, int(round(price)))

def _refresh_prices(market):
    params = market.get("params")
    for item in PRODUCTS:
        market["prices"][item] = market_price(item, market["inventory"][item], params)

def _new_plant(crop, day, turns_per_day):
    cd = CROPS[crop]
    return {
        "kind": "PLANT",
        "crop": crop,
        "planted_day": day,
        "watered_today": False,
        "consecutive_unwatered": 1,  # planting day counts as unwatered
        "yield_units": 0 if cd["ongoing"] else 1,
        "max_lifespan_step": (-1 if cd["ongoing"] else (day + cd["max_yield_day"] + 1) * turns_per_day),
        "fertilized_until_day": -1,
    }

def _new_animal(animal, day):
    a = ANIMALS[animal]
    return {
        "kind": a["structure"],
        "animal": animal,
        "placed_day": day,
        "yield_units": 0,
        "consecutive_unfed": 0,
        "fed_today": False,
        "cared_today": False,
        "fertilizer_available": False,
        "pending_care_bonus": 0,
    }

def _farmer_position(farm, idx):
    """idx 0 = main farmer, 1+ = hand index."""
    if idx == 0:
        return farm["farmer"]
    return farm["hands"][idx - 1] if idx - 1 < len(farm["hands"]) else None

def _set_farmer_position(farm, idx, pos):
    if idx == 0:
        farm["farmer"] = list(pos)
    else:
        farm["hands"][idx - 1] = list(pos)

def _farmer_inventory(private, idx):
    """Inventories list is [main_farmer, *hands]; grow it if idx is past the end."""
    while len(private["inventories"]) <= idx:
        private["inventories"].append({})
    return private["inventories"][idx]

def _inv_add(inv, item, n=1):
    inv[item] = inv.get(item, 0) + n

def _inv_take(inv, item, n=1):
    if inv.get(item, 0) < n:
        return False
    inv[item] -= n
    if inv[item] == 0:
        del inv[item]
    return True

def _apply_unit_action(farm, private, idx, action, board_size, day, turns_per_day, shed_capacity=100):
    """Process one farmer/hand's action. Invalid / illegal actions are silent no-ops."""
    if not isinstance(action, list) or not action:
        return
    op = action[0]
    pos = _farmer_position(farm, idx)
    if pos is None:
        return
    fx, fy = pos[0], pos[1]
    inv = _farmer_inventory(private, idx)

    if op in FARMER_MOVES:
        dx, dy = FARMER_MOVES[op]
        nx, ny = fx + dx, fy + dy
        if not (0 <= nx < board_size and 0 <= ny < board_size):
            return
        # Movement onto LOCKED tiles is allowed: a hand can spawn on a locked
        # shed-access tile, and blocking movement would strand it there forever.
        # Tile operations (PLANT, WATER, etc.) still no-op on LOCKED tiles.
        _set_farmer_position(farm, idx, (nx, ny))
        return

    if op == "PASS":
        return

    tile = farm["tiles"][fy][fx]

    # Shed operations resolve before the LOCKED guard. They use the tile only as
    # a standing position -- the shed itself is always owned -- and three of the
    # four shed-access tiles start LOCKED, so guarding them first would make the
    # shed unreachable from those tiles.
    if op == "DROP":
        if not _is_shed_adjacent((fx, fy), board_size):
            return
        shed = private["shed"]
        for item, n in list(inv.items()):
            if n <= 0:
                del inv[item]
                continue
            room = max(0, shed_capacity - sum(shed.values()))
            take = min(n, room)
            if take > 0:
                shed[item] = shed.get(item, 0) + take
            del inv[item]
        return

    if op == "PICKUP":
        if not _is_shed_adjacent((fx, fy), board_size):
            return
        if len(action) < 2:
            return
        item = action[1]
        n = int(action[2]) if len(action) >= 3 else 1
        if n <= 0:
            return
        # Seeds live in private["seeds"] and are consumed directly by PLANT;
        # they never pass through farmer inventory or the shed.
        available = private["shed"].get(item, 0)
        n = min(n, available)
        if n <= 0:
            return
        private["shed"][item] -= n
        _inv_add(inv, item, n)
        return

    if op == "PLACE":
        if len(action) < 2:
            return
        item = action[1]
        # Animal placement: standing on a matching unoccupied structure. A LOCKED
        # tile is the string "LOCKED", never a dict, so this branch cannot match
        # there and PLACE falls through to the shed path below.
        if (
            item in ANIMALS
            and isinstance(tile, dict)
            and tile.get("kind") == ANIMALS[item]["structure"]
            and "animal" not in tile
        ):
            if _inv_take(inv, item, 1):
                farm["tiles"][fy][fx] = _new_animal(item, day)
            return
        # Shed drop: orthogonally adjacent to the shed; obeys shedCapacity.
        if _is_shed_adjacent((fx, fy), board_size):
            n = int(action[2]) if len(action) >= 3 else 1
            if n <= 0:
                return
            n = min(n, inv.get(item, 0))
            if n <= 0:
                return
            current = sum(private["shed"].values())
            room = max(0, shed_capacity - current)
            n = min(n, room)
            if n <= 0:
                return
            inv[item] -= n
            if inv[item] == 0:
                del inv[item]
            private["shed"][item] = private["shed"].get(item, 0) + n
        return

    # Everything below mutates the tile the unit stands on, so it requires that
    # tile to be owned.
    if tile == "LOCKED":
        return

    if op == "PLANT":
        if len(action) < 2:
            return
        crop = action[1]
        if crop not in CROPS:
            return
        if tile is not None:
            return
        if private["seeds"].get(crop, 0) <= 0:
            return
        private["seeds"][crop] -= 1
        farm["tiles"][fy][fx] = _new_plant(crop, day, turns_per_day)
        return

    if op == "WATER":
        if not (isinstance(tile, dict) and tile.get("kind") == "PLANT"):
            return
        if tile["watered_today"]:
            return
        tile["watered_today"] = True
        crop_data = CROPS[tile["crop"]]
        if not crop_data["ongoing"]:
            age_days = day - tile["planted_day"]
            window_start = (crop_data["max_yield_day"] + 1) // 2
            if window_start <= age_days <= crop_data["max_yield_day"]:
                bonus = 2 if tile["fertilized_until_day"] >= day else 1
                tile["yield_units"] = min(crop_data["max_yield"], tile["yield_units"] + bonus)
        return

    if op == "HARVEST":
        if not isinstance(tile, dict):
            return
        if tile.get("yield_units", 0) <= 0:
            return
        if tile.get("kind") == "PLANT":
            crop_data = CROPS[tile["crop"]]
            if day - tile["planted_day"] < crop_data["first_yield_day"]:
                # Ongoing crops only accumulate yield_units after first_yield_day,
                # so reaching here with yield_units > 0 indicates a bug.
                if crop_data["ongoing"]:
                    print(
                        f"WARNING: HARVEST on immature ongoing {tile['crop']} "
                        f"(planted day {tile['planted_day']}, current day {day}, "
                        f"first_yield_day {crop_data['first_yield_day']}, "
                        f"yield_units {tile['yield_units']}); should never happen"
                    )
                return
            units = tile["yield_units"]
            tile["yield_units"] = 0
            _inv_add(inv, tile["crop"], units)
            if not crop_data["ongoing"]:
                farm["tiles"][fy][fx] = None
        elif "animal" in tile:
            units = tile["yield_units"]
            tile["yield_units"] = 0
            _inv_add(inv, ANIMALS[tile["animal"]]["product"], units)
        return

    if op == "FERTILIZE":
        if not (isinstance(tile, dict) and tile.get("kind") == "PLANT"):
            return
        if not _inv_take(inv, "FERTILIZER", 1):
            return
        # Active for `day`, `day+1`, `day+2` (3 days inclusive).
        tile["fertilized_until_day"] = max(tile.get("fertilized_until_day", -1), day + 2)
        return

    if op == "DIG":
        if tile is None:
            return
        # Removes plants, weeds, empty coop/pasture. Does NOT remove a placed animal.
        if isinstance(tile, dict) and "animal" in tile:
            return
        farm["tiles"][fy][fx] = None
        return

    if op == "BUILD_COOP":
        if tile is not None:
            return
        farm["tiles"][fy][fx] = {"kind": "COOP"}
        return

    if op == "BUILD_PASTURE":
        if tile is not None:
            return
        farm["tiles"][fy][fx] = {"kind": "PASTURE"}
        return

    if op == "FEED":
        if not (isinstance(tile, dict) and "animal" in tile):
            return
        if tile["fed_today"]:
            return
        if not _inv_take(inv, "WHEAT", 1):
            return
        tile["fed_today"] = True
        return

    if op == "COLLECT_FERTILIZER":
        if not (isinstance(tile, dict) and "animal" in tile):
            return
        if not tile["fertilizer_available"]:
            return
        tile["fertilizer_available"] = False
        _inv_add(inv, "FERTILIZER", 1)
        return

    if op == "CARE":
        if not (isinstance(tile, dict) and "animal" in tile):
            return
        if tile["cared_today"]:
            return
        tile["cared_today"] = True
        return

def _spawn_hand(farm, board_size):
    """First free shed-access tile (NWSE order); ties broken by min occupancy."""
    occupants = {tile: 0 for tile in _shed_access_tiles(board_size)}
    all_pos = [tuple(farm["farmer"])] + [tuple(p) for p in farm["hands"]]
    for pos in all_pos:
        if pos in occupants:
            occupants[pos] += 1
    best = sorted(occupants.items(), key=lambda kv: (kv[1], _shed_access_tiles(board_size).index(kv[0])))
    return list(best[0][0])

def _process_market(state, env):
    """Per-unit lockstep: at each step, quote both players' current-unit prices, then commit both."""
    obs0 = state[0].observation
    market = obs0.market
    farms = obs0.farms
    privates = [s.observation.private for s in state]
    board_size = int(get(env.configuration, "boardSize", 10))
    max_orders = max(1, int(get(env.configuration, "maxMarketOrdersPerTurn", 10)))
    hire_mult = int(get(env.configuration, "farmHandCostMult", FARM_HAND_COST_MULT))
    shed_capacity = int(get(env.configuration, "shedCapacity", 100))

    queues = []
    for s in state:
        action = s.action if isinstance(s.action, dict) else {}
        m = action.get("market", []) if isinstance(action, dict) else []
        q = list(m) if isinstance(m, list) else []
        queues.append(q[:max_orders])

    max_len = max((len(q) for q in queues), default=0)
    for i in range(max_len):
        order_states = []
        for player_id, q in enumerate(queues):
            ostate = None
            if i < len(q):
                ostate = _parse_order(q[i])
            order_states.append(ostate)

        # Atomic orders (HIRE, BUY_LAND): handle once, in player order.
        for player_id, ostate in enumerate(order_states):
            if ostate is None:
                continue
            op = ostate["type"]
            if op == "HIRE":
                _do_hire(farms[player_id], privates[player_id], board_size, hire_mult)
                order_states[player_id] = None
            elif op == "BUY_LAND":
                _do_buy_land(farms[player_id], board_size)
                order_states[player_id] = None

        # Per-unit lockstep loop for SELL / BUY_*.
        idx_esc = 0
        while True:
            idx_esc += 1
            if idx_esc >= 100_000:
                print("WARNING: kaggriculture market loop exceeded 100k iterations; aborting")
                break
            quoted = [None, None]
            for player_id, ostate in enumerate(order_states):
                if ostate is None or ostate["remaining"] <= 0:
                    continue
                op = ostate["type"]
                item = ostate["item"]
                if op == "SELL" and item in PRODUCTS:
                    quoted[player_id] = ("SELL", item, market_price(item, market["inventory"][item], market.get("params")), ostate)
                elif op == "BUY_PRODUCT" and item in ("WHEAT", "FERTILIZER"):
                    # Quote at post-buy inventory so a buy/sell round-trip
                    # against an unchanged market nets zero.
                    quoted[player_id] = ("BUY_PRODUCT", item, market_price(item, market["inventory"][item] - 1, market.get("params")), ostate)
                elif op == "BUY_SEED" and item in CROPS:
                    quoted[player_id] = ("BUY_SEED", item, CROPS[item]["seed"], ostate)
                elif op == "BUY_ANIMAL" and item in ANIMALS:
                    quoted[player_id] = ("BUY_ANIMAL", item, ANIMALS[item]["cost"], ostate)
                else:
                    order_states[player_id] = None  # malformed sub-op; abort

            if all(q is None for q in quoted):
                break

            # Both players see the same pre-commit inventory for this unit.
            committed_any = False
            for player_id, q in enumerate(quoted):
                if q is None:
                    continue
                op, item, price, ostate = q
                ok = _commit_unit(op, item, price, farms[player_id], privates[player_id], market, shed_capacity)
                if ok:
                    ostate["remaining"] -= 1
                    committed_any = True
                else:
                    order_states[player_id] = None  # can't continue this order

            if not committed_any:
                break

        _refresh_prices(market)

def _parse_order(order):
    if not isinstance(order, list) or not order:
        return None
    op = order[0]
    if op == "HIRE":
        return {"type": "HIRE"}
    if op == "BUY_LAND":
        return {"type": "BUY_LAND"}
    if op in ("BUY_SEED", "BUY_PRODUCT", "BUY_ANIMAL", "SELL"):
        if len(order) < 3:
            return None
        try:
            n = int(order[2])
        except (TypeError, ValueError):
            return None
        if n <= 0:
            return None
        return {"type": op, "item": order[1], "remaining": n}
    return None

def _commit_unit(op, item, price, farm, private, market, shed_capacity=100):
    if op == "SELL":
        if private["shed"].get(item, 0) <= 0:
            return False
        private["shed"][item] -= 1
        farm["money"] += price
        # Sales at $1 do not increase market supply.
        if price > 1:
            market["inventory"][item] += 1
        return True
    if op == "BUY_PRODUCT":
        if farm["money"] < price:
            return False
        # Bought goods land in the shed, which obeys shedCapacity like every
        # other deposit path (pickup, shed-drop, end-of-day drop).
        if sum(private["shed"].values()) >= shed_capacity:
            return False
        farm["money"] -= price
        private["shed"][item] = private["shed"].get(item, 0) + 1
        market["inventory"][item] -= 1
        return True
    if op == "BUY_SEED":
        if farm["money"] < price:
            return False
        farm["money"] -= price
        private["seeds"][item] = private["seeds"].get(item, 0) + 1
        return True
    if op == "BUY_ANIMAL":
        if farm["money"] < price:
            return False
        if sum(private["shed"].values()) >= shed_capacity:
            return False
        farm["money"] -= price
        private["shed"][item] = private["shed"].get(item, 0) + 1
        return True
    return False

def _fib(n):
    """Indexed so _fib(0)=1, _fib(1)=1, _fib(2)=2, _fib(3)=3, _fib(4)=5..."""
    a, b = 1, 1
    for _ in range(n):
        a, b = b, a + b
    return a

def _hire_cost(n_already_today, mult=FARM_HAND_COST_MULT):
    return mult * _fib(n_already_today)

def _do_hire(farm, private, board_size, mult=FARM_HAND_COST_MULT):
    cost = _hire_cost(farm["hires_today"], mult)
    if farm["money"] < cost:
        return
    farm["money"] -= cost
    farm["hires_today"] += 1
    farm["hands"].append(_spawn_hand(farm, board_size))
    private["inventories"].append({})

def _do_buy_land(farm, board_size):
    n_unlocked_extra = len(farm["unlocked_quadrants"]) - 1  # NW is always there
    if n_unlocked_extra >= len(LAND_ORDER):
        return
    cost = LAND_PRICES[n_unlocked_extra]
    if farm["money"] < cost:
        return
    farm["money"] -= cost
    quadrant = LAND_ORDER[n_unlocked_extra]
    farm["unlocked_quadrants"].append(quadrant)
    for y in range(board_size):
        for x in range(board_size):
            if _quadrant_of(x, y, board_size) == quadrant and farm["tiles"][y][x] == "LOCKED":
                farm["tiles"][y][x] = None

def _town_consume(env, state, step):
    obs0 = state[0].observation
    market = obs0.market
    town = obs0.town
    cfg = env.configuration
    shop_interval = max(1, int(get(cfg, "townShopSellInterval", 4)))
    center_interval = max(1, int(get(cfg, "townCenterSellInterval", 24)))

    if step % shop_interval == 0:
        # unlocked_shops may list the same shop more than once (shops are drawn
        # with replacement); each instance consumes independently.
        for shop_name in town.get("unlocked_shops", []):
            products = SHOPS[shop_name]
            multiplier = 2 if len(products) == 1 else 1
            for item in products:
                market["inventory"][item] -= multiplier

    if step % center_interval == 0:
        for item in TOWN_CENTER_PRODUCTS:
            market["inventory"][item] -= 1

    _refresh_prices(market)

def _decay_plants(farm, step):
    board_size = len(farm["tiles"])
    for y in range(board_size):
        for x in range(board_size):
            tile = farm["tiles"][y][x]
            if not isinstance(tile, dict) or tile.get("kind") != "PLANT":
                continue
            mls = tile["max_lifespan_step"]
            if mls < 0 or step < mls:
                continue
            if (step - mls) % 2 != 0:
                continue
            tile["yield_units"] -= 1
            if tile["yield_units"] <= 0:
                farm["tiles"][y][x] = {"kind": "WEED"}

def _daily_refresh_plants(farm, current_day, turns_per_day):
    board_size = len(farm["tiles"])
    next_day = current_day + 1
    for y in range(board_size):
        for x in range(board_size):
            tile = farm["tiles"][y][x]
            if not isinstance(tile, dict) or tile.get("kind") != "PLANT":
                continue
            was_watered = tile["watered_today"]
            if was_watered:
                tile["consecutive_unwatered"] = 0
            else:
                tile["consecutive_unwatered"] += 1
            tile["watered_today"] = False
            if tile["consecutive_unwatered"] >= 2:
                farm["tiles"][y][x] = {"kind": "WEED"}
                continue
            cd = CROPS[tile["crop"]]
            if not cd["ongoing"]:
                continue
            days_since_first = next_day - tile["planted_day"] - cd["first_yield_day"]
            if days_since_first < 0:
                continue
            interval = cd["interval"]
            if days_since_first % interval != 0:
                continue
            production_count = days_since_first // interval + 1
            if production_count > cd["max_yield"]:
                continue
            # Fertilizer bonus only applies on watered days (basic needs first).
            fertilized = was_watered and tile.get("fertilized_until_day", -1) >= current_day
            tile["yield_units"] = min(cd["max_yield"], tile["yield_units"] + (2 if fertilized else 1))
            if production_count == cd["max_yield"]:
                tile["max_lifespan_step"] = (next_day + 1) * turns_per_day

def _daily_refresh_animals(farm, day):
    board_size = len(farm["tiles"])
    next_day = day + 1
    for y in range(board_size):
        for x in range(board_size):
            tile = farm["tiles"][y][x]
            if not (isinstance(tile, dict) and "animal" in tile):
                continue
            if tile["fed_today"]:
                tile["consecutive_unfed"] = 0
            else:
                tile["consecutive_unfed"] += 1
            if tile["consecutive_unfed"] >= 2:
                # Animal escapes; structure remains.
                farm["tiles"][y][x] = {"kind": ANIMALS[tile["animal"]]["structure"]}
                continue
            a = ANIMALS[tile["animal"]]
            days_since_first = next_day - tile["placed_day"] - a["first_yield_day"]
            if days_since_first >= 0 and days_since_first % a["interval"] == 0:
                base = 1
                # Care bonus only consumed on a fed production day.
                bonus = tile.pop("pending_care_bonus", 0) if tile["fed_today"] else 0
                tile["yield_units"] = min(a["max_held"], tile["yield_units"] + base + bonus)
                tile["pending_care_bonus"] = 0
            if tile["cared_today"] and tile["fed_today"]:
                tile["pending_care_bonus"] = tile.get("pending_care_bonus", 0) + 1
            tile["fertilizer_available"] = True
            tile["fed_today"] = False
            tile["cared_today"] = False

def _drop_inventories_to_shed(private, capacity):
    """Drop every per-farmer inventory into the shed up to `capacity`; overflow is discarded.
    Seeds are tracked separately in private["seeds"] and don't pass through the shed."""
    shed = private["shed"]
    for inv in private["inventories"]:
        for item, n in list(inv.items()):
            if n <= 0:
                del inv[item]
                continue
            current = sum(v for k, v in shed.items())
            room = max(0, capacity - current)
            take = min(n, room)
            if take > 0:
                shed[item] = shed.get(item, 0) + take
            del inv[item]
