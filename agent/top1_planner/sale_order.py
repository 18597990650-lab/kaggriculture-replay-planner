"""Prioritize the sales whose current price band is most exposed to new supply."""
from . import rules


def price_impact(item, quantity, market):
    """Quote one equal-sized intervening supply batch, respecting the price floor.

    This is a product sensitivity score, not an estimate of rival inventory.
    Quantities, reservations and financing decisions belong to the caller.
    """
    inventory = market['inventory'][item]
    receipts = []
    for batch in range(2):
        value = 0
        for _ in range(max(0, int(quantity))):
            price = rules.market_price(item, inventory, market.get('params'))
            value += price
            inventory += price > 1
        receipts.append(value)
    return receipts[0]-receipts[1]


def rank_sales(orders, market):
    """Reorder only sales; preserve every purchase boundary and every quantity."""
    result = [list(order) for order in orders]
    start = 0
    while start < len(result):
        if result[start][0] != 'SELL':
            start += 1
            continue
        end = start+1
        while end < len(result) and result[end][0] == 'SELL':
            end += 1
        result[start:end] = sorted(result[start:end], key=lambda o:-price_impact(o[1], o[2], market))
        start = end
    return result
