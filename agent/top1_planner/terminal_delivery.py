"""Return sale goods by their last physically feasible warehouse deadline."""

from . import rules
from .compiler import distance, nearest_shed, walk


def last_chance_delivery(tick, farm, private, actor, command, market, capacity):
    """Protect carried goods when another detour would miss the final market.

    Actor work happens before market orders. Reaching warehouse access and
    unloading require distinct actions; an unload at 718 can still be sold.
    Capacity is checked after earlier actors' work in the same action phase.
    """
    if tick < 696:
        return command, None
    bag = private['inventories'][actor]
    goods = {p:q for p,q in bag.items() if p in rules.PRODUCTS and q > 0}
    if not goods:
        return command, None
    point = tuple(rules._farmer_position(farm, actor))
    access = nearest_shed(point)
    travel = distance(point, access)
    remaining = 719-tick
    if remaining > travel+1:
        return command, None
    room = max(0, capacity-sum(private['shed'].values()))
    chosen = command
    if travel:
        chosen = walk(point, access)
    elif room >= sum(bag.values()):
        chosen = ['DROP']
    elif room:
        def immediate_receipts(item):
            inventory = market['inventory'][item]
            total = 0
            for _ in range(min(room, goods[item])):
                price = rules.market_price(item, inventory, market.get('params'))
                total += price
                inventory += price > 1
            return total
        item = max(goods, key=lambda p:(immediate_receipts(p), p))
        chosen = ['PLACE', item, min(room, goods[item])]
    if chosen == command:
        return command, None
    return chosen, dict(step=tick, actor=actor, original_command=command,
        command=chosen, carried_sale_goods=goods, position=list(point),
        nearest_access=list(access), actions_remaining=remaining,
        minimum_delivery_actions=travel+1, warehouse_room=room,
        feasible_before_action=remaining >= travel+1)


def final_orders(private):
    """No later actor action can consume a purchase or a reservation."""
    return [['SELL', item, private['shed'][item]] for item in rules.PRODUCTS
            if private['shed'].get(item,0) > 0]
