"""Bound short-horizon trade means with an explicitly estimated goods supply."""
PRODUCTS=('WHEAT','CARROT','TOMATO','STRAWBERRY','MELON','EGG','MILK','WOOL','FERTILIZER')
HORIZONS=(4,8,24)


def project(slots,public,stock,observation):
    step=int(observation['step'])
    if step%24 or stock is None or stock['origin']!=step:
        return None
    if len(slots)!=6 or any(len(row)!=9 for row in slots):
        raise ValueError('Short-horizon quantity forecast requires six four-hour trade slots')
    if tuple(stock['products'])!=PRODUCTS[1:8]:
        raise ValueError('Stock and short-horizon product order differ')
    capacity=[]
    for pi,point in enumerate(stock['point_estimate'],1):
        allowance=2*public['assets'][pi] if pi in (1,4) else 0
        capacity.append(max(0.,point+public['ready'][pi]+allowance))
    raw=[]
    points=[]
    total=[0.]*9
    for hour,slot in enumerate(slots):
        total=[a+b for a,b in zip(total,slot)]
        if hour in (0,1,5):
            raw.append(tuple(total))
            points.append(tuple(min(max(0.,q),capacity[pi-1]) if 0<pi<8 else q
                                for pi,q in enumerate(total)))
    return dict(products=PRODUCTS,horizons=HORIZONS,point_estimate=tuple(points),
        raw_neighbor_mean=tuple(raw),estimated_goods_capacity=tuple(capacity),origin=step,
        evidence='Dawn cumulative SELL minus BUY, including one-dollar trades. Public stock point plus visible yields and carrot/melon watering allowance is an estimated capacity, not a certified upper bound. No market-price response is simulated.')
