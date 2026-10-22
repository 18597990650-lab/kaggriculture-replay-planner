"""Explicit hypothetical public market flows for programme comparison."""
from . import rules
PRODUCTS = tuple(rules.PRODUCTS)

class ScenarioRules:
    """Use real own actions and prices against declared external market flows."""
    def __init__(self, seat, start, shops=(), flow=None, scale=1.0, *, consumption=None, record_units=False):
        self.seat = seat
        self.start = start
        self.shops = shops
        self.flow = flow or {}
        self.scale = scale
        self.step = start
        self.accepted = {}
        self.rival_value = 0
        self.consumption=consumption or {}
        self.prepared_step=None
        self.unit_rounds={} if record_units else None

    def __getattr__(self, name):
        return getattr(rules, name)

    def prepare(self,observation,configuration):
        """Expose the same declared current town before policy and market phases."""
        self._prepare_town(observation['town'],int(observation['step']),configuration)

    def _prepare_town(self,town,tick,configuration):
        if self.prepared_step==tick:
            return
        self.prepared_step=tick
        unlock = int(configuration['townShopUnlockInterval']) * 24
        if tick > self.start and tick % unlock == 0 and len(town['unlocked_shops']) < 8:
            index = tick // unlock - self.start // unlock - 1
            if index < len(self.shops):
                town['unlocked_shops'].append(self.shops[index])
        if self.consumption:
            town['_forecast_shop_consumption']=dict(self.consumption.get(tick//4*4,{}))

    def _town_consume(self,env,states,tick):
        rules._town_consume(env,states,tick)
        extra=self.consumption.get(tick,{})
        if extra:
            market=states[0].observation.market
            for item,quantity in extra.items():
                market['inventory'][item]-=quantity
            rules._refresh_prices(market)

    def _process_market(self, states, env):
        tick = self.step
        other = 1 - self.seat
        town = states[0].observation.town
        self._prepare_town(town,tick,env.configuration)
        external = self.flow.get(tick, {})
        rival = states[other].observation.private
        rival['shed'] = {}
        orders = []
        for item, count in external.items():
            amount = round(abs(count) * self.scale)
            if not amount:
                continue
            if count < 0:
                orders.append(['SELL', item, amount])
                rival['shed'][item] = amount
            elif item in ('WHEAT', 'FERTILIZER'):
                orders.append(['BUY_PRODUCT', item, amount])
        states[other].action = dict(farmer=['PASS'], hands=[], market=orders)
        other_farm = states[0].observation.farms[other]
        other_farm['money'] = 1_000_000_000
        before = dict(states[self.seat].observation.private['shed'])
        if self.unit_rounds is None:
            rules._process_market(states, env)
        else:
            rounds=[]
            rules._process_market(states, env, on_round=rounds.append)
            if rounds:
                self.unit_rounds[tick]=rounds
        self.rival_value += other_farm['money'] - 1_000_000_000
        after = states[self.seat].observation.private['shed']
        self.accepted[tick] = {p: after.get(p, 0)-before.get(p, 0)
                               for p in PRODUCTS if after.get(p, 0) != before.get(p, 0)}
        self.step += 1


    def can_reuse_own_market(self):
        """A completed unilateral order phase is reusable only without rival orders."""
        for item,count in self.flow.get(self.step,{}).items():
            if round(abs(count)*self.scale) and (count<0 or item in ('WHEAT','FERTILIZER')):
                return False
        return True

    def reuse_own_market(self,states,configuration,before,rounds):
        if not self.can_reuse_own_market():
            raise ValueError('A unilateral market phase cannot skip rival orders')
        tick=self.step
        self._prepare_town(states[0].observation.town,tick,configuration)
        states[0].observation.farms[1-self.seat]['money']=1_000_000_000
        after=states[self.seat].observation.private['shed']
        self.accepted[tick]={p:after.get(p,0)-before.get(p,0)
            for p in PRODUCTS if after.get(p,0)!=before.get(p,0)}
        if self.unit_rounds is not None and rounds:
            self.unit_rounds[tick]=rounds
        self.step+=1
