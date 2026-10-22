"""Explicit funded alternatives to one known source's imminent sale timing."""
from copy import deepcopy


class SaleTimingScenario:
    """Keep legal source work and vary only sales of a declared commodity.

    Stock is never injected. Delayed requests remain subject to real warehouse
    inventory and native market capacity. The scenario is not a probability
    estimate or a bound on all future policies.
    """
    def __init__(self, source, item, mode):
        if mode not in ('omit_target_sales', 'delay_one_tick'):
            raise ValueError('Unknown sale timing scenario')
        self.source, self.item, self.mode = source, item, mode
        self.pending = 0

    def fork(self):
        if self.pending:
            raise ValueError('Entry scenarios must fork before their first action')
        return SaleTimingScenario(self.source.fork(), self.item, self.mode)

    def __call__(self, observation, configuration):
        action = deepcopy(self.source(observation, configuration))
        current = 0
        positions = []
        for index, order in enumerate(action['market'][:10]):
            if len(order)>=3 and order[:2]==['SELL',self.item]:
                current += max(0,int(order[2]))
                positions.append(index)
                order[2] = 0
        if self.mode == 'delay_one_tick':
            if self.pending:
                if positions:
                    action['market'][positions[0]][2] = self.pending
                    self.pending = 0
                elif len(action['market'])<10:
                    action['market'].append(['SELL',self.item,self.pending])
                    self.pending = 0
            self.pending += current
        return action
