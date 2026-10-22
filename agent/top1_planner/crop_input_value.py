"""Match optional fertilizer to actual water/production windows and sale dates."""
from functools import lru_cache

from . import rules
from .compiler import distance,nearest_shed


def fertilizer_due(tile,day):
    """One unit must add yield to today's service, beyond already paid fertilizer."""
    if not isinstance(tile,dict) or tile.get('crop') not in rules.CROPS:
        return False
    if tile.get('fertilized_until_day',-1)>=day:
        return False
    data=rules.CROPS[tile['crop']]
    age=day-tile['planted_day']
    if data['ongoing']:
        first=age+1-data['first_yield_day']
        return day<29 and first>=0 and first%data['interval']==0 and first//data['interval']<data['max_yield']
    if tile.get('watered_today'):
        return False
    return (tile['yield_units']+1<data['max_yield']
        and (data['max_yield_day']+1)//2<=age<=data['max_yield_day'])


@lru_cache(maxsize=1024)
def crop_service(item,generation,end,fertilized):
    """Price the same finite harvest deadline and optional productive input visits."""
    data=rules.CROPS[item]
    farm=dict(tiles=[[rules._new_plant(item,generation,24)]],farmer=[0,0],hands=[])
    private=dict(inventories=[{}],shed={},seeds={})
    rows=[]
    for day in range(generation,end+1):
        tile=farm['tiles'][0][0]
        if not isinstance(tile,dict) or tile.get('crop')!=item:
            break
        fertilizer=int(fertilized and fertilizer_due(tile,day))
        if fertilizer:
            private['inventories'][0]['FERTILIZER']=1
            rules._apply_unit_action(farm,private,0,['FERTILIZE'],1,day,24,100)
        rules._apply_unit_action(farm,private,0,['WATER'],1,day,24,100)
        before=private['inventories'][0].get(item,0)
        if day==end or data['ongoing'] and day-generation>=data['first_yield_day']:
            rules._apply_unit_action(farm,private,0,['HARVEST'],1,day,24,100)
        harvested=private['inventories'][0].get(item,0)-before
        rows.append((day,harvested,fertilizer))
        rules._daily_refresh_plants(farm,day,24)
    return tuple(rows)


def crop_input_value(point,item,cycles,price,fertilized):
    """Biological screening value; the full paired executor prices labor and displacement."""
    value=0
    travel=distance(point,nearest_shed(point))+3
    for generation,end in cycles:
        value-=rules.CROPS[item]['seed']
        for day,harvest,fertilizer in crop_service(item,generation,end,fertilized):
            if harvest:
                value+=price(item,day*24+travel,harvest)
            if fertilizer:
                value-=price('FERTILIZER',day*24,1)
    return value
