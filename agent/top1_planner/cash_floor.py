"""Public cash floor-sale bounds including visible outlays and known own fills."""
from collections import Counter
import math

RESOURCES=frozenset(('WHEAT','FERTILIZER'))


def own_resource_activity(own_state,action,configuration,rules):
    action=action if isinstance(action,dict) else {}
    orders=action.get('market',[]);orders=orders if isinstance(orders,list) else []
    total=Counter(own_state['private']['shed'])
    for bag in own_state['private']['inventories']:total.update(bag)
    for order in orders[:int(configuration['maxMarketOrdersPerTurn'])]:
        parsed=rules._parse_order(order)
        if not parsed or parsed.get('item') not in RESOURCES:continue
        if parsed['type']=='BUY_PRODUCT':return True
        if parsed['type']=='SELL' and total[parsed['item']]>0:return True
    return False


def visible_outlays(old,current,step,configuration,rules):
    cost=0
    if (step+1)%int(configuration['turnsPerDay']):
        assert current['hires_today']>=old['hires_today']
        cost+=sum(rules._hire_cost(n,int(configuration['farmHandCostMult']))
                  for n in range(old['hires_today'],current['hires_today']))
    before,after=len(old['unlocked_quadrants'])-1,len(current['unlocked_quadrants'])-1
    assert after>=before
    cost+=sum(rules.LAND_PRICES[n] for n in range(before,after))
    return cost


def floor_sale_lower(old,observation,own_resources,configuration,rules,own_sales,own_sales_exact):
    """A lower bound remains valid without knowing hidden seeds or animal buys.

    Known hire/land costs are added to visible cash gain. A rival's resource
    net purchase makes its other-receipts upper bound negative when our own
    resource trades cannot interfere. For non-resource products that never
    reach the floor, known successful own sales are removed from the shared
    inventory addition before bounding rival receipts at the initial price.
    """
    if own_resources:return {},dict(status='own_resource_interference')
    target=1-int(observation['player']);current=observation['farms'][target];cash_gain=current['money']-old['farm']['money'];step=old['step']
    known_cost=visible_outlays(old['farm'],current,step,configuration,rules)
    consumed=Counter()
    if step%int(configuration['townShopSellInterval'])==0:
        for shop in old['shops']:
            products=rules.SHOPS[shop]
            for item in products:consumed[item]+=2 if len(products)==1 else 1
    if step%int(configuration['townCenterSellInterval'])==0:consumed.update({p:1 for p in rules.TOWN_CENTER_PRODUCTS})
    other_upper=0;possible=[];capacity=int(configuration['shedCapacity'])
    for item in rules.PRODUCTS:
        previous=old['inventory'][item];addition=observation['market']['inventory'][item]-previous+consumed[item];price=rules.market_price(item,previous,old['params'])
        after_price=rules.market_price(item,previous+addition,old['params'])
        if item in RESOURCES:
            other_upper+=addition*price
            possible_inventory=previous+capacity
        else:
            assert addition>=0
            own=own_sales[item] if own_sales_exact and after_price>1 else 0
            assert addition>=own
            other_upper+=(addition-own)*price
            possible_inventory=previous+addition
        if price==1 or rules.market_price(item,possible_inventory,old['params'])==1:possible.append(item)
    lower=max(0,math.ceil(cash_gain+known_cost-other_upper))
    evidence=dict(status='unique' if lower and len(possible)==1 else 'ambiguous_or_zero',cash_gain=cash_gain,known_outlays=known_cost,
                  other_net_receipts_upper=other_upper,aggregate_floor_lower=lower,possible_floor_products=possible)
    return ({possible[0]:lower} if lower and len(possible)==1 else {}),evidence


def make_cash_observer(base,rules):
    class CashFloorSupplyBelief(base):
        def __init__(self,configuration):
            super().__init__(configuration)
            self.floor_configuration={k:int(configuration[k]) for k in
                ('maxMarketOrdersPerTurn','shedCapacity','townShopSellInterval','townCenterSellInterval','turnsPerDay','farmHandCostMult')}
            self.own_resources=None;self.floor_evidence=None;self.floor_lower={}

        def observe(self,observation):
            previous=self.previous;value=super().observe(observation);self.floor_lower={};self.floor_evidence=None
            if previous is not None:
                if self.own_resources is None:raise RuntimeError('Own prior action is required for cash-floor attribution')
                self.floor_lower,self.floor_evidence=floor_sale_lower(previous,observation,self.own_resources,self.floor_configuration,rules,self.sold_upper,self.own_sale_evidence['exact'])
                for item,lower in self.floor_lower.items():
                    if item in RESOURCES:continue
                    self.pending[item]=max(0,self.pending[item]-lower)
                    if self.pending[item]==0:self.uncertain.discard(item)
                value['quantities']={p:q for p,q in self.pending.items() if q and p not in self.uncertain}
                value['uncertain_upper']={p:q for p,q in self.pending.items() if q and p in self.uncertain}
            return value

        def remember_action(self,action):
            super().remember_action(action)
            self.own_resources=own_resource_activity(self.own_state,action,self.floor_configuration,rules)

    return CashFloorSupplyBelief
