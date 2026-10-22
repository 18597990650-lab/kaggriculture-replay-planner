"""Native resource accounting for a declared rival policy, with public fit checks."""
from collections import Counter
from dataclasses import dataclass
from types import FunctionType, SimpleNamespace as NS

from . import native_rules as rules


def clone_farm(farm):
    return dict(farm,farmer=list(farm['farmer']),hands=[list(p) for p in farm['hands']],
        unlocked_quadrants=list(farm['unlocked_quadrants']),
        tiles=[[dict(t) if isinstance(t,dict) else t for t in row] for row in farm['tiles']])


def clone_private(private):
    return dict(private,shed=dict(private['shed']),seeds=dict(private['seeds']),
                inventories=[dict(bag) for bag in private['inventories']])


def clone_market(market):
    return dict(market,inventory=dict(market['inventory']),prices=dict(market['prices']))

PUBLIC_KEYS = ('step', 'day', 'hour', 'farms', 'market', 'town')
CONFIG_KEYS = ('boardSize', 'turnsPerDay', 'shedCapacity', 'maxMarketOrdersPerTurn',
               'farmHandCostMult', 'townShopSellInterval', 'townCenterSellInterval',
               'townShopUnlockInterval', 'episodeSteps', 'actTimeout', 'runTimeout',
               'agentTimeout', 'startingMoney', 'weedSpawnChance')


def stock(private):
    result = Counter(private['shed'])
    for bag in private['inventories']:
        result.update(bag)
    return result


def difference(after, before):
    return {key: after.get(key, 0)-before.get(key, 0) for key in set(after) | set(before)
            if after.get(key, 0) != before.get(key, 0)}


def public_copy(observation):
    return dict(step=int(observation['step']), day=int(observation['step'])//24,
                hour=int(observation['step'])%24,
                farms=[clone_farm(farm) for farm in observation['farms']],
                market=clone_market(observation['market']),
                town=dict(unlocked_shops=list(observation['town']['unlocked_shops'])))


def field_identity(tile):
    if not isinstance(tile, dict):
        return None
    if tile.get('animal'):
        return (tile['animal'], tile['placed_day'])
    if tile.get('crop'):
        return (tile['crop'], tile['planted_day'])
    return None


@dataclass
class Transition:
    public: dict
    privates: list
    transactions: list
    resources: list
    field_losses: list
    cash_residuals: list
    resource_residuals: list


def public_fit(observation, transition, observer):
    tick = int(observation['step'])
    predicted = transition.public
    expected = predicted['farms'][1-observer]
    actual = observation['farms'][1-observer]
    tile_errors, new_weeds = [], []
    for y, row in enumerate(expected['tiles']):
        for x, tile in enumerate(row):
            later = actual['tiles'][y][x]
            if tile == later:
                continue
            if tick % 24 == 0 and tile is None and later == {'kind': 'WEED'}:
                new_weeds.append([x, y])
            else:
                tile_errors.append(dict(point=[x, y], expected=tile, actual=later))
    fit = dict(step=tick,
        cash=[observation['farms'][s]['money']-predicted['farms'][s]['money'] for s in (0, 1)],
        positions=(actual['farmer']!=expected['farmer'] or actual['hands']!=expected['hands']),
        land=actual['unlocked_quadrants']!=expected['unlocked_quadrants'],
        hires=actual['hires_today']!=expected['hires_today'],
        tiles=tile_errors, observed_new_weeds=new_weeds,
        market=difference(observation['market']['inventory'], predicted['market']['inventory']),
        own_private_matches=observation['private']==transition.privates[observer])
    fit['error'] = (sum(abs(n) for n in fit['cash'])/100 + len(tile_errors)
        + sum(abs(n) for n in fit['market'].values()) + int(fit['positions'])
        + int(fit['land'])+int(fit['hires'])+int(not fit['own_private_matches']))
    return fit


def advance(observation, privates, actions, configuration):
    """Execute one legal step; future random weeds and new shops are not sampled.

    Both private states and both actions are explicit hypotheses or own facts.
    The isolated market function records successful transactions without
    mutating the shared native rules module.
    """
    cfg = {key: configuration[key] for key in CONFIG_KEYS if key in configuration}
    for key, value in (('boardSize', 10), ('turnsPerDay', 24), ('shedCapacity', 100),
                       ('maxMarketOrdersPerTurn', 10)):
        if cfg[key] != value:
            raise ValueError('Unsupported ledger configuration: '+key)
    now = public_copy(observation)
    tick = now['step']
    if not 0 <= tick < 719:
        raise ValueError('No game action exists at this step')
    farms, market = now['farms'], now['market']
    privates = [clone_private(private) for private in privates]
    initial = [stock(p) for p in privates]
    initial_cash = [farm['money'] for farm in farms]
    resources, transactions, field_losses = [], [], []

    def resource_event(seat, phase, before, after, **detail):
        delta = difference(after, before)
        if delta:
            resources.append(dict(seat=seat, phase=phase, delta=delta, **detail))

    for seat in (0, 1):
        private, farm = privates[seat], farms[seat]
        commands = [actions[seat].get('farmer', ['PASS']), *actions[seat].get('hands', [])]
        requested = Counter(cmd[1] for cmd in commands if len(cmd)>1 and cmd[0]=='PLANT')
        blocked = {crop for crop, n in requested.items() if n > private['seeds'].get(crop, 0)}
        for actor, command in enumerate(commands):
            if actor > len(farm['hands']):
                continue
            before = stock(private)
            point = list(rules._farmer_position(farm, actor))
            effective = ['PASS'] if command[:1] == ['PLANT'] and command[1] in blocked else command
            rules._apply_unit_action(farm, private, actor, effective, 10, tick//24, 24, 100)
            resource_event(seat, 'actor', before, stock(private), actor=actor,
                           operation=effective, point=point)

    def seat_of(farm):
        return 0 if farm is farms[0] else 1

    def commit(op, item, price, farm, private, mkt, capacity=100):
        before = stock(private)
        ok = rules._commit_unit(op, item, price, farm, private, mkt, capacity)
        if ok:
            seat = seat_of(farm)
            transactions.append(dict(seat=seat, operation=op, item=item, quantity=1,
                                     cash=price if op=='SELL' else -price))
            resource_event(seat, 'market', before, stock(private), operation=op, item=item)
        return ok

    def hire(farm, private, size, multiplier=1):
        before = farm['money']
        rules._do_hire(farm, private, size, multiplier)
        if before != farm['money']:
            transactions.append(dict(seat=seat_of(farm), operation='HIRE', item='HAND',
                                     quantity=1, cash=farm['money']-before))

    def land(farm, size):
        before = farm['money']
        rules._do_buy_land(farm, size)
        if before != farm['money']:
            transactions.append(dict(seat=seat_of(farm), operation='BUY_LAND', item='LAND',
                                     quantity=1, cash=farm['money']-before))

    namespace = dict(rules._process_market.__globals__, _commit_unit=commit,
                     _do_hire=hire, _do_buy_land=land)
    process_market = FunctionType(rules._process_market.__code__, namespace,
                                   argdefs=rules._process_market.__defaults__)
    states = [NS(action=actions[s], observation=NS(farms=farms, private=privates[s],
              market=market, town=now['town'])) for s in (0, 1)]
    environment = NS(configuration=cfg)
    process_market(states, environment)
    rules._town_consume(environment, states, tick)
    for seat, farm in enumerate(farms):
        fields = {(x, y): dict(tile) for y, row in enumerate(farm['tiles'])
                  for x, tile in enumerate(row) if field_identity(tile)}
        rules._decay_plants(farm, tick)
        if tick % 24 == 23:
            rules._daily_refresh_plants(farm, tick//24, 24)
            rules._daily_refresh_animals(farm, tick//24)
            before = stock(privates[seat])
            rules._drop_inventories_to_shed(privates[seat], 100)
            resource_event(seat, 'midnight_overflow', before, stock(privates[seat]))
            farm.update(farmer=list(rules._default_spawn(10)), hands=[], hires_today=0)
            privates[seat]['inventories'] = [{}]
        for (x, y), tile in fields.items():
            later = farm['tiles'][y][x]
            if field_identity(later) != field_identity(tile):
                item = rules.ANIMALS[tile['animal']]['product'] if tile.get('animal') else tile['crop']
                field_losses.append(dict(seat=seat, point=[x, y],
                    kind='escape' if tile.get('animal') else 'wilt_or_decay',
                    identity=field_identity(tile), item=item, quantity=tile.get('yield_units', 0)))
    cash_residuals, resource_residuals = [], []
    for seat in (0, 1):
        cash_residuals.append(farms[seat]['money']-initial_cash[seat]
                             -sum(row['cash'] for row in transactions if row['seat']==seat))
        expected = initial[seat].copy()
        for row in resources:
            if row['seat'] == seat:
                expected.update(row['delta'])
        resource_residuals.append(difference(stock(privates[seat]), expected))
    if any(cash_residuals) or any(resource_residuals):
        raise AssertionError(('Unclosed hypothesis ledger', cash_residuals, resource_residuals))
    now.update(step=tick+1, day=(tick+1)//24, hour=(tick+1)%24)
    return Transition(now, privates, transactions, resources, field_losses,
                      cash_residuals, resource_residuals)


class RouteHypothesis:
    """A continuous private ledger with explicit public contradictions.

    Observed public state conditions the next policy call, but never patches
    hidden goods. A contradicted ledger remains marked inconsistent.
    """
    def __init__(self, name, policy, configuration):
        self.name, self.policy = name, policy
        self.cfg = {key: configuration[key] for key in CONFIG_KEYS if key in configuration}
        self.private = rules._new_private()
        self.previous = None
        self.transition = None
        self.mismatches = []
        self.last_fit = None
        self.consistent = True
        self.steps = 0
        self.score = 0.0
        self.own_action = None
        self.rival_action = None
        self.private_before = None

    def observe(self, observation):
        tick = int(observation['step'])
        observer = int(observation['player'])
        if self.previous is None:
            if tick != 0:
                raise ValueError('A route inventory hypothesis must start at the known opening')
        else:
            if self.transition is None or tick != self.previous['step']+1:
                raise ValueError('Consecutive observations and one action per step are required')
            fit = public_fit(observation, self.transition, observer)
            error = fit['error']
            self.last_fit = fit
            self.score += error
            if error:
                self.consistent = False
                self.mismatches.append(fit)
        own_private = observation['private']
        if self.transition is not None and self.transition.privates[observer] == own_private:
            own_private = self.transition.privates[observer]
        self.previous = public_copy(observation)
        self.previous['player'] = observer
        self.previous['private'] = clone_private(own_private)
        self.transition = None
        return dict(name=self.name, step=tick, consistent=self.consistent,
                    cumulative_public_error=self.score, private=clone_private(self.private))

    def act_and_advance(self, own_action, *, own_transform=None):
        if self.previous is None or self.transition is not None:
            raise ValueError('Observe exactly once before advancing')
        observer = self.previous['player']
        rival = 1-observer
        hypothetical = public_copy(self.previous)
        hypothetical.update(player=rival, private=clone_private(self.private))
        rival_action = self.policy(hypothetical, self.cfg)
        if own_transform is not None:
            own_action = own_transform(self.previous, self.cfg, own_action, clone_private(self.private), rival_action)
        self.own_action, self.rival_action = own_action, rival_action
        self.private_before = clone_private(self.private)
        actions = [own_action, rival_action] if observer==0 else [rival_action, own_action]
        privates = [self.previous['private'], self.private] if observer==0 else [self.private, self.previous['private']]
        self.transition = advance(self.previous, privates, actions, self.cfg)
        self.private = self.transition.privates[rival]
        self.steps += 1
        return rival_action, self.transition
