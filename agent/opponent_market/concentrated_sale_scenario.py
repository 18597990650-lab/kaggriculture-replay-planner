"""A funded opponent may concentrate stored output into the recovery market."""
from copy import deepcopy


class ConcentratedSaleScenario:
    def __init__(self, source, item):
        if item in ('WHEAT','FERTILIZER'):
            raise ValueError('This scenario liquidates only non-input sale goods')
        self.source, self.item, self.phase = source, item, 0

    def fork(self):
        if self.phase:
            raise ValueError('Fork the scenario before its first action')
        return ConcentratedSaleScenario(self.source.fork(), self.item)

    def __call__(self, observation, configuration):
        action = deepcopy(self.source(observation, configuration))
        limit = int(configuration['maxMarketOrdersPerTurn'])
        orders = action['market']
        positions = [i for i,o in enumerate(orders[:limit]) if len(o)>=3 and o[:2]==['SELL',self.item]]
        if self.phase==0:
            for index in positions:
                orders[index][2]=0
        elif self.phase==1:
            quantity = int(configuration['shedCapacity'])
            if positions:
                orders[positions[0]][2]=quantity
                for index in positions[1:]:
                    orders[index][2]=0
            elif len(orders)<limit:
                orders.insert(0,['SELL',self.item,quantity])
        self.phase+=1
        return action
