"""Resolve day-end harvests only when all compatible public states agree."""


def midnight_harvest(tile, later, step, actors, rules):
    """Return a uniquely implied harvest quantity or None for unresolved cases.

    HARVEST takes all available yield. A day-end production refresh can mask
    that quantity when held yield was capped. Public starvation counters and
    fertilizer dates constrain this turn's service actions; actor count limits
    how many of those actions and a harvest could have happened together.
    """
    if (step%24!=23 or actors<=0 or not isinstance(later,dict)
            or tile.get('yield_units',0)<=0):
        return None
    day=step//24
    if tile.get('animal'):
        if (later.get('animal')!=tile['animal']
                or later.get('placed_day')!=tile['placed_day']):
            return None
        fed=later.get('consecutive_unfed')==0
        if tile['fed_today'] and not fed:
            return None
        if not fed and later.get('consecutive_unfed')!=tile['consecutive_unfed']+1:
            return None
        feed_ops=[] if tile['fed_today']==fed else [['FEED']]
        service_sets=[feed_ops]
        if not tile['cared_today']:
            service_sets.append(feed_ops+[['CARE']])
    elif tile.get('crop'):
        product=tile['crop']
        if (not rules.CROPS[product]['ongoing'] or later.get('crop')!=product
                or later.get('planted_day')!=tile['planted_day']
                or day-tile['planted_day']<rules.CROPS[product]['first_yield_day']):
            return None
        watered=later.get('consecutive_unwatered')==0
        if tile['watered_today'] and not watered:
            return None
        if not watered and later.get('consecutive_unwatered')!=tile['consecutive_unwatered']+1:
            return None
        operations=[] if tile['watered_today']==watered else [['WATER']]
        if later.get('fertilized_until_day')!=tile['fertilized_until_day']:
            if later.get('fertilized_until_day')!=max(tile['fertilized_until_day'],day+2):
                return None
            operations.append(['FERTILIZE'])
        service_sets=[operations]
    else:
        return None
    possible=set()
    for services in service_sets:
        for harvest in (False,True):
            operations=services+([['HARVEST']] if harvest else [])
            if len(operations)>actors:
                continue
            farm=dict(farmer=[0,0],hands=[],tiles=[[dict(tile)]])
            private=dict(shed={},seeds={},inventories=[{'WHEAT':1,'FERTILIZER':1}])
            for command in operations:
                rules._apply_unit_action(farm,private,0,command,1,day,24)
            rules._decay_plants(farm,step)
            rules._daily_refresh_plants(farm,day,24)
            rules._daily_refresh_animals(farm,day)
            if farm['tiles'][0][0]==later:
                possible.add(tile['yield_units'] if harvest else 0)
    return next(iter(possible)) if len(possible)==1 else None
