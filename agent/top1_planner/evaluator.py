"""Value arbitrary complete production programmes from the real paid state."""
from dataclasses import dataclass, field
from copy import deepcopy
from collections.abc import Mapping, Sequence
import math

from . import rules
from .material import project
from .markets import ScenarioRules
from .warehouse import close


@dataclass(frozen=True)
class MarketWorld:
    """A declared hypothesis, not a claim about the rival's hidden inventory."""
    future_shops: tuple[str, ...] = ()
    external_flow: Mapping[int, Mapping[str, int]] = field(default_factory=dict)
    scale: float = 1.0
    public_farms: Mapping[int, dict] = field(default_factory=dict)
    additional_consumption: Mapping[int, Mapping[str, float]] = field(default_factory=dict)
    label: str = 'declared_market_world'
    uncertain_shop_demand: bool = False


@dataclass(frozen=True)
class ProgramValue:
    cash: float
    minimum_cash: float
    rival_net_cash: float
    harvest: dict
    net_sold_units: dict
    net_bought_units: dict
    lost_units: dict
    physical_issues: tuple
    terminal: dict


def evaluate(observation: dict, configuration: dict, programme: Sequence[dict], world: MarketWorld, *, close_warehouse: bool = False) -> ProgramValue:
    """Evaluate a full programme without changing paid state or input commands.

    Programme indices are absolute ticks; exactly 719 decisions are required.
    Own actors, production, market receipts and night refresh use the inherited
    physical rules. The rival follows only the explicitly supplied market flow.
    Future weeds and adaptive future replanning are absent from this quote.
    """
    required = {'boardSize':10, 'turnsPerDay':24, 'shedCapacity':100,
                'maxMarketOrdersPerTurn':10, 'townShopUnlockInterval':3}
    for name, value in required.items():
        if name not in configuration or configuration[name] != value:
            raise ValueError('Unsupported or missing configuration: ' + name)
    if len(programme) != 719:
        raise ValueError('A complete programme must contain 719 decisions')
    start = int(observation['step'])
    if not 0 <= start <= 718 or int(observation['player']) not in (0,1):
        raise ValueError('Invalid paid-state entry')
    if world.scale < 0 or any(shop not in rules.SHOPS for shop in world.future_shops):
        raise ValueError('Invalid declared market world')
    for tick, goods in world.external_flow.items():
        if not start <= tick < 719 or any(item not in rules.PRODUCTS or not isinstance(q, int) for item, q in goods.items()):
            raise ValueError('External flow must use future ticks and integer public goods')
    for tick, goods in world.additional_consumption.items():
        if not start<=tick<719 or any(item not in rules.PRODUCTS or not isinstance(q,(int,float))
                or not math.isfinite(q) or q<0 for item,q in goods.items()):
            raise ValueError('Additional consumption must be finite future public demand')
    tape = deepcopy(list(programme))
    for action in tape:
        if set(action) != {'farmer','hands','market'}:
            raise ValueError('Incomplete programme action')
        if len(action['market']) > configuration['maxMarketOrdersPerTurn']:
            raise ValueError('Programme exceeds the order limit')
    tape.append(dict(farmer=['PASS'], hands=[], market=[]))
    model = ScenarioRules(int(observation['player']), start, world.future_shops, world.external_flow, world.scale,
                          consumption=world.additional_consumption)
    transform = (lambda own, action: close(own, action, tape, configuration, rules)) if close_warehouse else None
    result = project(observation, tape, configuration, model, fund=False, record=False, transform=transform)
    return ProgramValue(cash=result['cash'], minimum_cash=result['minimum_cash'],
        rival_net_cash=model.rival_value, harvest=result['harvest'], net_sold_units=result['sales_net'],
        net_bought_units=result['purchases_net'], lost_units=result['lost'],
        physical_issues=tuple(result['issues']), terminal=result['final'])
