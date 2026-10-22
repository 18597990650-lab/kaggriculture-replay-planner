"""Use a completed parent's inventory policy for inexpensive physical screening."""
from . import rules
from .settlement import _quotes
from .sale_order import rank_sales


class ReferenceSettlement:
    """Keep parent storage targets subject to each candidate's cash and capacity."""

    def __init__(self, compiled):
        self.hold = {r['step']: {h['item']: h['quantity'] for h in r['held']}
                     for r in compiled.result['settlement_records']}

    def __call__(self, observation, farm, private, market, reserves, cash_required, *, incoming_purchase_units=0):
        amounts = {p: max(0, private['shed'].get(p, 0)-reserves.get(p, 0)) for p in rules.PRODUCTS}
        target = self.hold.get(int(observation['step']), {})
        held = {p: min(q, amounts[p]) for p, q in target.items() if q and amounts[p]}
        capacity = max(0, 100-sum(sum(b.values()) for b in private['inventories'])-sum(reserves.values())
                       -sum(q for p, q in private['shed'].items() if p not in rules.PRODUCTS)-incoming_purchase_units)
        cash, inventories = farm['money'], {}
        book = _quotes(market)
        for item, quantity in amounts.items():
            inventory = market['inventory'][item]
            for _ in range(quantity-held.get(item, 0)):
                price = book[item].quote(inventory)
                cash += price
                inventory += price > 1
            inventories[item] = inventory
        occupied = sum(held.values())
        for _, item, _ in rank_sales([['SELL', p, q] for p, q in held.items()], market):
            inventory = inventories[item]
            while held[item] and (cash < cash_required or occupied > capacity):
                price = book[item].quote(inventory)
                held[item] -= 1
                occupied -= 1
                cash += price
                inventory += price > 1
        return {p: q-held.get(p, 0) for p, q in amounts.items()}, None
