"""Admit an idle-worker survival repair only after pricing its full continuation."""
from collections import Counter
from copy import deepcopy
import time

from .demand_value import value_trade_tape
from .markets import ScenarioRules
from .material import project
from .worlds import public_worlds


class QuoteExpired(Exception):
    pass


def _quote(observation, configuration, controller, world, deadline):
    began=time.perf_counter()
    seat, start = int(observation['player']), int(observation['step'])
    model = ScenarioRules(seat, start, world.future_shops, world.external_flow, world.scale,
                          consumption=world.additional_consumption, record_units=world.uncertain_shop_demand)
    route = [dict(farmer=['PASS'], hands=[], market=[]) for _ in range(720)]

    def choose(own, unused):
        if time.perf_counter() >= deadline:
            raise QuoteExpired()
        model.prepare(own, configuration)
        if own['step'] in world.public_farms:
            own['farms'][1-seat] = deepcopy(world.public_farms[own['step']])
        return controller.action(own)

    result = project(observation, route, configuration, model, fund=False, record=False, transform=choose)
    physical_seconds=time.perf_counter()-began
    if time.perf_counter() >= deadline:
        raise QuoteExpired()
    own, rival = result['cash'], model.rival_value
    if model.unit_rounds is not None:
        valued = value_trade_tape(observation, configuration, model.unit_rounds, own_cash=own, rival_cash=rival)
        own, rival = valued['own_cash'], valued['rival_market']
    if time.perf_counter() >= deadline:
        raise QuoteExpired()
    return dict(own_cash=own, rival_market=rival, relative=own-rival,
                raw_own_cash=result['cash'], raw_relative=result['cash']-model.rival_value,
                issues=dict(Counter(row['code'] for row in result['issues'])),
                lost=result['lost'], harvest=result['harvest'], minimum_cash=result['minimum_cash'],
                repairs=[row for row in controller.dispatch_records if row.get('kind')=='survival_water'],
                physical_seconds=physical_seconds,seconds=time.perf_counter()-began,
                end_step=719)


class SurvivalAdmission:
    """Use the turn-entry paid state for both branches; inner quotes cannot recurse."""

    def __init__(self, observation, configuration, controller, deadline):
        self.observation = observation
        self.configuration = configuration
        self.controller = controller.fork()
        self.deadline = deadline
        self.events = []

    def __call__(self, proposal):
        start = time.perf_counter()
        event = dict(kind='SURVIVAL_ADMISSION', step=int(self.observation['step']),
                     point=proposal['point'], item=proposal['item'], generation=proposal['generation'],
                     admitted=False, completed=False,
                     basis='Same paid entry through719, declared public supply and future-shop demand; no future observations or opponent private inventory.')
        try:
            if self.events:
                event['reason'] = 'one_paid_entry_branch_per_turn'
                return False
            if self.deadline-start < .10:
                raise QuoteExpired()
            world = public_worlds(self.observation, future_model='uniform_mean')[1]
            event['world_seconds']=time.perf_counter()-start
            old = self.controller.fork()
            new = self.controller.fork()
            key = (tuple(proposal['point']), proposal['item'], proposal['generation'])
            day = int(self.observation['step']) // 24
            old.survival_approved.pop(key, None)
            new.survival_approved[key] = day
            baseline = _quote(self.observation, self.configuration, old, world, self.deadline)
            candidate = _quote(self.observation, self.configuration, new, world, self.deadline)
            own_delta = candidate['own_cash'] - baseline['own_cash']
            relative_delta = candidate['relative'] - baseline['relative']
            raw_delta = candidate['raw_own_cash'] - baseline['raw_own_cash']
            resource_safe = not (Counter(candidate['issues'])-Counter(baseline['issues'])) and not (
                Counter(candidate['lost'])-Counter(baseline['lost']))
            activated = any(tuple(row['point']) == key[0] and row['item'] == key[1]
                            and row['generation'] == key[2] and row['step']//24 == day
                            for row in candidate['repairs'])
            admitted = activated and own_delta > 0 and relative_delta >= 0 and raw_delta >= 0 and resource_safe
            event.update(completed=True, baseline=baseline, candidate=candidate,
                         own_delta=own_delta, relative_delta=relative_delta, raw_own_delta=raw_delta,
                         resource_safe=resource_safe, activated=activated, admitted=admitted,
                         reason='full_continuation_positive' if admitted else 'full_continuation_not_better')
        except QuoteExpired:
            event['reason'] = 'insufficient_complete_quote_budget'
        finally:
            event['seconds'] = time.perf_counter()-start
            self.events.append(event)
        return event['admitted']
