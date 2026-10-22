"""Public upper bounds on additional non-purchasable goods harvested today."""
from top1_planner import rules

PRODUCTS=tuple(rules.PRODUCTS)
INDEX={p:i for i,p in enumerate(PRODUCTS)}


def harvest_upper(observation):
    step=int(observation['step'])
    if step%24:
        return None
    day=step//24
    values=[0]*len(PRODUCTS)
    farm=observation['farms'][1-int(observation['player'])]
    for row in farm['tiles']:
        for tile in row:
            if not isinstance(tile,dict):
                continue
            if tile.get('animal'):
                product=rules.ANIMALS[tile['animal']]['product']
                values[INDEX[product]]+=max(0,tile['yield_units'])
            elif tile.get('kind')=='PLANT' and tile['crop']!='WHEAT':
                product=tile['crop'];crop=rules.CROPS[product]
                age=day-tile['planted_day']
                if age<crop['first_yield_day']:
                    continue
                quantity=max(0,tile['yield_units'])
                if not crop['ongoing'] and not tile['watered_today']:
                    start=(crop['max_yield_day']+1)//2
                    if start<=age<=crop['max_yield_day']:
                        quantity=min(crop['max_yield'],quantity+2)
                values[INDEX[product]]+=quantity
    return tuple(values)


def constrain(quantity,harvest,stock):
    if quantity is None:
        return
    if harvest is None or stock is None or quantity['origin']!=stock['origin']:
        raise ValueError('Maturity constraints require current dawn stock and harvest estimates')
    capacity=tuple(point+harvest[p] for p,point in enumerate(stock['point_estimate'],1))
    before=quantity['point_estimate']
    quantity['point_estimate']=tuple(tuple(min(value,capacity[p-1]) if 0<p<8 else value
                                          for p,value in enumerate(row)) for row in before)
    quantity['before_maturity_limit']=before
    quantity['estimated_mature_goods_capacity']=capacity
    quantity['public_day_harvest_upper']=harvest
    quantity['maturity_boundary']='Harvest term follows public maturity and watering rules; stock is still estimated, so their sum is not a guaranteed upper bound.'
