"""Bound our non-resource sales using our state and this turn's unit work."""
from collections import Counter


def snapshot(observation):
    """Copy only the local state needed to resolve one own unit phase."""
    farm = observation['farms'][int(observation['player'])]
    private = observation['private']
    return dict(
        step=int(observation['step']),
        farm=dict(farm, farmer=list(farm['farmer']),
                  hands=[list(p) for p in farm['hands']],
                  tiles=[[dict(t) if isinstance(t, dict) else t for t in row]
                         for row in farm['tiles']]),
        private=dict(private, shed=dict(private['shed']),
                     seeds=dict(private['seeds']),
                     inventories=[dict(bag) for bag in private['inventories']]))


def bound_own_sales(state, action, configuration, rules):
    """Return a per-good upper bound; no opponent action or private data enters.

    Goods other than wheat and fertilizer cannot be bought or consumed from
    the warehouse. Their successful sales are the smaller of all valid sell
    requests and post-unit warehouse stock. A full DROP can depend on bag key
    order, which archived JSON need not preserve. Those transitions retain the
    looser pre-turn total-stock bound instead of assuming an order.
    """
    goods = tuple(p for p in rules.PRODUCTS if p not in ('WHEAT', 'FERTILIZER'))
    action = action if isinstance(action, dict) else {}
    orders = action.get('market', [])
    orders = orders if isinstance(orders, list) else []
    requested = Counter()
    for order in orders[:int(configuration['maxMarketOrdersPerTurn'])]:
        parsed = rules._parse_order(order)
        if parsed and parsed['type'] == 'SELL' and parsed['item'] in goods:
            requested[parsed['item']] += parsed['remaining']
    if not requested:
        return Counter(), dict(exact=True, overflow_drop=False, requested={})

    farm0, private0 = state['farm'], state['private']
    farm = dict(farm0, farmer=list(farm0['farmer']),
                hands=[list(p) for p in farm0['hands']],
                tiles=[[dict(t) if isinstance(t, dict) else t for t in row]
                       for row in farm0['tiles']])
    private = dict(private0, shed=dict(private0['shed']),
                   seeds=dict(private0['seeds']),
                   inventories=[dict(bag) for bag in private0['inventories']])
    total = Counter(private['shed'])
    for bag in private['inventories']:
        total.update(bag)
    hands = action.get('hands', [])
    hands = hands if isinstance(hands, list) else []
    units = [action.get('farmer', ['PASS']), *hands]
    demand = Counter(a[1] for a in units
                     if isinstance(a, list) and len(a) >= 2 and a[0] == 'PLANT')
    blocked = {p for p, n in demand.items() if n > private['seeds'].get(p, 0)}
    board = int(configuration['boardSize'])
    capacity = int(configuration['shedCapacity'])
    day_length = int(configuration['turnsPerDay'])
    overflow = False
    for actor, command in enumerate(units):
        pos = rules._farmer_position(farm, actor)
        if (pos is not None and isinstance(command, list) and command
                and command[0] == 'DROP' and rules._is_shed_adjacent(tuple(pos), board)):
            bag = rules._farmer_inventory(private, actor)
            positive = [n for n in bag.values() if n > 0]
            room = max(0, capacity - sum(private['shed'].values()))
            overflow |= len(positive) > 1 and 0 < room < sum(positive)
        if (isinstance(command, list) and len(command) >= 2
                and command[0] == 'PLANT' and command[1] in blocked):
            command = ['PASS']
        rules._apply_unit_action(farm, private, actor, command, board,
                                 state['step'] // day_length, day_length, capacity)
    available = total if overflow else private['shed']
    upper = Counter({p: min(n, max(0, available.get(p, 0))) for p, n in requested.items()})
    return upper, dict(exact=not overflow, overflow_drop=bool(overflow), requested=dict(requested))


def make_observer(base, rules):
    class OwnSaleSupplyBelief(base):
        def __init__(self, configuration):
            super().__init__()
            self.configuration = {k: int(configuration[k]) for k in
                                  ('boardSize', 'shedCapacity', 'turnsPerDay', 'maxMarketOrdersPerTurn')}
            self.own_state = None
            self.own_sale_evidence = None

        def observe(self, observation):
            self.own_state = snapshot(observation)
            return super().observe(observation)

        def remember_action(self, action):
            if self.own_state is None:
                raise RuntimeError('Observe our state before projecting our sales')
            self.sold_upper, self.own_sale_evidence = bound_own_sales(
                self.own_state, action, self.configuration, rules)

    return OwnSaleSupplyBelief
