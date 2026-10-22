"""Apply remaining certified sale-only P110 supply to the current day."""
from dataclasses import replace
from top1_planner.p110_sale import active_prediction, GOODS

def intraday_world(observation, world):
    """Use the current public sale debit instead of the previous dawn quantity."""
    prediction = active_prediction(observation)
    if prediction is None:
        return world, dict(active=False, reason='No valid observed dawn prediction')
    start = int(observation['step'])
    if prediction['asof'] != start:
        raise ValueError('Current-day supply requires the latest certified public sales')
    flow = {tick: dict(batch) for tick, batch in world.external_flow.items()}
    for tick in list(flow):
        if start <= tick < prediction['end']:
            for item in GOODS:
                flow[tick].pop(item, None)
            if not flow[tick]:
                del flow[tick]
    for tick, batch in prediction['flow'].items():
        if not start <= tick < prediction['end']:
            continue
        for item, value in batch.items():
            if item not in GOODS or value > 0 or int(value) != value:
                raise ValueError('P110 intraday supply must be integer sale-only goods')
            if value:
                flow.setdefault(tick, {})[item] = value
    if world.scale != 1:
        raise ValueError('Calibrated P110 quantities cannot be rescaled')
    return replace(world, external_flow=flow), dict(active=True, origin=prediction['origin'],
        asof=start, end=prediction['end'], remaining_debits=prediction['remaining_debits'])
