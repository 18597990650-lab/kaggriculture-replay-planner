"""Preserve calibrated features while refining the physical inventory bound."""
from . import rules
from resource_observer import ResourceFlowObserver
from grain_balance import WheatBalance
from goods_observer import GoodsFlowObserver
from .belief import PublicSupplyBelief
from .own_sale_bound import make_observer

from .refined_belief import PublicSupplyBelief as RefinedPublicBelief

OwnSaleSupplyBelief = make_observer(RefinedPublicBelief, rules)
from .cash_floor import make_cash_observer
CashFloorSupplyBelief = make_cash_observer(OwnSaleSupplyBelief, rules)


class DualSupplyBelief(OwnSaleSupplyBelief):
    def __init__(self, configuration):
        super().__init__(configuration)
        self.legacy = PublicSupplyBelief()
        self.cash_floor = CashFloorSupplyBelief(configuration)
        self.resources = ResourceFlowObserver(configuration)
        self.grain = WheatBalance()
        self.goods = GoodsFlowObserver(configuration)

    def observe(self, observation):
        corrected = super().observe(observation)
        legacy = self.legacy.observe(observation)
        legacy['own_sale_corrected'] = corrected
        legacy['cash_floor_corrected'] = self.cash_floor.observe(observation)
        flow = self.resources.observe(observation)
        history = list(self.resources.history)
        flow['completed_wheat_windows'] = tuple(sum(row[0] for row in history[:window]) for window in (4,8,24)) if int(observation['step'])%24==0 and len(history)==24 and all(row[0] is not None for row in history) else None
        flow['completed_fertilizer_windows'] = tuple(sum(row[1] for row in history[:window]) for window in (4,8,24)) if int(observation['step'])%24==0 and len(history)==24 and all(row[1] is not None for row in history) else None
        legacy['resource_flow'] = flow
        legacy['wheat_balance'] = self.grain.observe(observation, flow)
        legacy['goods_flow'] = self.goods.observe(observation,self.sold_upper,self.own_sale_evidence)
        return legacy

    def remember_action(self, action):
        super().remember_action(action)
        self.legacy.remember_action(action)
        self.cash_floor.remember_action(action)
        self.resources.remember_action(action)
