"""Map a six-bin conditional supply forecast into two shared order windows."""
PAIRS = tuple((a,b) for a in range(6) for b in range(a+1,6))
DISTANCE = tuple(tuple(min(abs(t-a),abs(t-b)) for t in range(6)) for a,b in PAIRS)


def two_slots(slots, prices):
    mass = [sum(abs(q)*price for q,price in zip(row,prices)) for row in slots]
    best = min(range(15), key=lambda i: sum(q*d for q,d in zip(mass,DISTANCE[i])))
    a,b = PAIRS[best]
    values = [[0.]*9,[0.]*9]
    for i, row in enumerate(slots):
        target = 0 if abs(i-a)<=abs(i-b) else 1
        for p,q in enumerate(row):
            values[target][p] += q
    return (a,b), values


def market_batches(slots, daily, prices, weight):
    pair, values = two_slots(slots,prices)
    weights = [weight]*9 if isinstance(weight,(int,float)) else weight
    assert len(weights)==9
    first = [int(round(-w*q)) for w,q in zip(weights,values[0])]
    target = [int(round(-w*q)) for w,q in zip(weights,daily)]
    second = [q-a for q,a in zip(target,first)]
    assert all(a+b==q for a,b,q in zip(first,second,target))
    return ((pair[0]*4+2,first),(pair[1]*4+2,second))
