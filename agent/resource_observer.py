"""Infer qualified resource transactions from public markets and our state."""
from collections import Counter, deque
from top1_planner import rules
from top1_planner.own_sale_bound import snapshot
RESOURCES=("WHEAT","FERTILIZER")

def total_resources(private):
    result=Counter(private['shed'])
    for bag in private['inventories']:
        result.update(bag)
    return tuple(result[p] for p in RESOURCES)

def project_own_unit_phase(state,action,configuration):
    action=action if isinstance(action,dict) else {}
    hands=action.get('hands',[]);hands=hands if isinstance(hands,list) else []
    units=[action.get('farmer',['PASS']),*hands]
    resource_ops={'HARVEST','COLLECT_FERTILIZER','FEED','FERTILIZE','DROP'}
    if not any(isinstance(unit,list) and unit and unit[0] in resource_ops for unit in units):
        return total_resources(state['private']),dict(status='resource_preserving_units')
    farm0,private0=state['farm'],state['private']
    farm=dict(farm0,farmer=list(farm0['farmer']),hands=[list(p) for p in farm0['hands']],
              tiles=[[dict(t) if isinstance(t,dict) else t for t in row] for row in farm0['tiles']])
    private=dict(private0,shed=dict(private0['shed']),seeds=dict(private0['seeds']),
                 inventories=[dict(bag) for bag in private0['inventories']])
    demands=Counter(unit[1] for unit in units if isinstance(unit,list) and len(unit)>=2 and unit[0]=='PLANT')
    blocked={item for item,count in demands.items() if count>private['seeds'].get(item,0)}
    board=configuration['boardSize'];capacity=configuration['shedCapacity'];day_length=configuration['turnsPerDay']
    for actor,unit in enumerate(units):
        position=rules._farmer_position(farm,actor)
        if position is not None and isinstance(unit,list) and unit and unit[0]=='DROP' and rules._is_shed_adjacent(tuple(position),board):
            bag=rules._farmer_inventory(private,actor)
            positive=[count for count in bag.values() if count>0]
            room=max(0,capacity-sum(private['shed'].values()))
            if len(positive)>1 and 0<room<sum(positive):
                return None,dict(status='ambiguous_drop_key_order')
        if isinstance(unit,list) and len(unit)>=2 and unit[0]=='PLANT' and unit[1] in blocked:
            unit=['PASS']
        rules._apply_unit_action(farm,private,actor,unit,board,state['step']//day_length,day_length,capacity)
    return total_resources(private),dict(status='projected_own_units')

class ResourceFlowObserver:
    def __init__(self, configuration):
        self.configuration = {key: int(configuration[key]) for key in
            ('boardSize', 'shedCapacity', 'turnsPerDay', 'townShopSellInterval',
             'townCenterSellInterval', 'maxMarketOrdersPerTurn')}
        self.previous = None
        self.own_observation = None
        self.projected_total = None
        self.projection_evidence = None
        self.own_resource_active = (False, False)
        self.own_resource_sell = (False, False)
        self.history = deque(maxlen=24)
        self.started_at_zero = False

    def observe(self, observation):
        tick = int(observation['step'])
        last = (None, None)
        reasons = ['initial', 'initial']
        if self.previous is None:
            self.started_at_zero = tick == 0
        else:
            old = self.previous
            if tick != old['step'] + 1:
                raise ValueError('Resource flow inference requires consecutive observations')
            if self.projection_evidence is None:
                raise RuntimeError('Remember our own action before inferring its resource flow')
            consumption = Counter()
            if old['step'] % self.configuration['townShopSellInterval'] == 0:
                for shop in old['shops']:
                    products = rules.SHOPS[shop]
                    for product in products:
                        consumption[product] += 2 if len(products) == 1 else 1
            if old['step'] % self.configuration['townCenterSellInterval'] == 0:
                consumption.update({product: 1 for product in rules.TOWN_CENTER_PRODUCTS})
            ours_after = total_resources(observation['private']) if self.projected_total is not None else None
            midnight = tick % self.configuration['turnsPerDay'] == 0
            # Market orders precede the midnight deposit. A warehouse below
            # capacity afterwards proves that this deposit discarded nothing.
            possible_overflow = sum(observation['private']['shed'].values()) >= self.configuration['shedCapacity']
            values = []
            reasons = []
            for pi, product in enumerate(RESOURCES):
                peak = old['inventory'][product] + self.configuration['shedCapacity'] * (1 + int(self.own_resource_sell[pi]))
                if rules.market_price(product, peak, old['params']) <= 1:
                    values.append(None)
                    reasons.append('floor_price_possible')
                    continue
                if not self.own_resource_active[pi]:
                    own_net = 0
                    reason = 'identified_no_own_order'
                elif midnight and possible_overflow:
                    values.append(None)
                    reasons.append('midnight_discard_ambiguity')
                    continue
                elif self.projected_total is None:
                    values.append(None)
                    reasons.append(self.projection_evidence['status'])
                    continue
                else:
                    own_net = self.projected_total[pi] - ours_after[pi]
                    reason = 'identified_midnight_no_overflow' if midnight else 'identified_projected'
                total_net = observation['market']['inventory'][product] - old['inventory'][product] + consumption[product]
                values.append(total_net - own_net)
                reasons.append(reason)
            last = tuple(values)
            self.history.append(last)
        known, count, sell, buy, known8, count8 = [], [], [], [], [], []
        recent_history = list(self.history)[-8:]
        for pi in range(2):
            observed = [record[pi] for record in self.history if record[pi] is not None]
            known.append(sum(observed))
            count.append(len(observed))
            sell.append(sum(max(0, value) for value in observed))
            buy.append(sum(max(0, -value) for value in observed))
            recent = [record[pi] for record in recent_history if record[pi] is not None]
            known8.append(sum(recent))
            count8.append(len(recent))
        self.previous = dict(step=tick, inventory=dict(observation['market']['inventory']),
                             params=observation['market'].get('params'),
                             shops=tuple(observation['town']['unlocked_shops']))
        self.own_observation = observation
        self.projection_evidence = None
        return dict(origin=tick, last_transaction=last, reasons=tuple(reasons),
                    known_net_last24=tuple(known), known_sell_last24=tuple(sell),
                    known_buy_last24=tuple(buy), observed_steps_last24=tuple(count),
                    known_net_last8=tuple(known8), observed_steps_last8=tuple(count8),
                    window_steps=len(self.history), started_at_zero=self.started_at_zero)

    def remember_action(self, action):
        if self.own_observation is None:
            raise RuntimeError('Observe our own state before projecting the unit phase')
        action = action if isinstance(action, dict) else {}
        orders = action.get('market', [])
        orders = orders if isinstance(orders, list) else []
        active, sold = set(), set()
        for order in orders[:max(1, self.configuration['maxMarketOrdersPerTurn'])]:
            parsed = rules._parse_order(order)
            if parsed and parsed['type'] in ('SELL', 'BUY_PRODUCT') and parsed['item'] in RESOURCES:
                active.add(parsed['item'])
                if parsed['type'] == 'SELL':
                    sold.add(parsed['item'])
        self.own_resource_active = tuple(product in active for product in RESOURCES)
        self.own_resource_sell = tuple(product in sold for product in RESOURCES)
        if not active:
            self.projected_total = None
            self.projection_evidence = dict(status='no_own_resource_orders')
        else:
            self.projected_total, self.projection_evidence = project_own_unit_phase(
                snapshot(self.own_observation), action, self.configuration)
