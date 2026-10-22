"""Net successful trade events at dawn, conditional on an initialized public observer."""
import gzip,hashlib,json,math
PRODUCTS=('WHEAT','CARROT','TOMATO','STRAWBERRY','MELON','EGG','MILK','WOOL','FERTILIZER')
BASE=(25,35,60,120,250,50,160,200,100)


def load_model(directory,record):
    raw=(directory/record['file']).read_bytes()
    if hashlib.sha256(raw).hexdigest()!=record['sha256']:raise ValueError('Pressure model hash mismatch')
    model=json.loads(gzip.decompress(raw))
    if model['schema']!='public-pressure-stock-v2' or len(model['linear'])!=12 or len(model['priors'])!=22:
        raise ValueError('Unsupported public pressure model')
    if not all(math.isfinite(v) for v in model['linear']+[model['offset']]):raise ValueError('Invalid pressure coefficients')
    return model


def observation_key(observation):
    if int(observation['step'])%24:return None
    cargo=observation.get('_public_rival_cargo')
    cargo=cargo.get('own_sale_corrected') if cargo is not None else None
    if cargo is None or not cargo['started_at_zero'] or cargo['origin']!=int(observation['step']):return None
    return tuple(cargo['quantities'].get(p,0) for p in PRODUCTS)+tuple(cargo['uncertain_upper'].get(p,0) for p in PRODUCTS)


def logit(value):
    p=min(.999,max(.001,value));return math.log(p/(1-p))


def coherent(values):
    for horizon in values:
        for product in range(9):
            for direction in range(2):horizon[direction][1][product]=min(horizon[direction][0][product],horizon[direction][1][product])
            total=max(1.,horizon[0][0][product]+horizon[1][0][product])
            for direction in range(2):
                for threshold in range(2):horizon[direction][threshold][product]/=total
    return values


def stock_features(cargo,stock):
    point=[0.,*stock['point_estimate'],0.]
    removed=[max(0.,cargo[p]+cargo[p+9]-point[p]) if 0<p<8 else 0. for p in range(9)]
    return tuple((math.log1p(a/6),math.log1p(b/6)) for a,b in zip(point,removed))


def project(model,rows,weights,distances,observation,day,stock):
    cargo=observation_key(observation)
    if cargo is None or stock is None:return None
    extra=stock_features(cargo,stock)
    minimum=math.log1p(min(distances));average=math.log1p(sum(d*w for d,w in zip(distances,weights)))
    known=[math.log1p(v/6) for v in cargo[:9]];upper=[math.log1p(v/6) for v in cargo[9:]];price=[math.log1p(observation['market']['prices'][p]/b) for p,b in zip(PRODUCTS,BASE)]
    raw=[];predicted=[]
    for hi,window in enumerate((1,2,6)):
        quantities=[[row[p] if window==6 else sum(row[9+b*9+p] for b in range(window)) for p in range(9)] for row in rows]
        source=[];calibrated=[]
        for di,direction in enumerate((1,-1)):
            source_direction=[];target_direction=[]
            for ti,threshold in enumerate((1,6)):
                probabilities=[];values=[]
                for p in range(9):
                    probability=sum(w for row,w in zip(quantities,weights) if direction*row[p]>=threshold);probabilities.append(probability)
                    score=logit(probability);prior=logit(model['priors'][day-6][hi][di][ti][p]);features=(1.,score,prior,minimum,average,(score-prior)*average,known[p],upper[p],price[p],float(direction),*extra[p])
                    value=model['offset']+sum(a*b for a,b in zip(model['linear'],features));values.append(1/(1+math.exp(-min(40,max(-40,value)))))
                source_direction.append(probabilities);target_direction.append(values)
            source.append(source_direction);calibrated.append(target_direction)
        raw.append(source);predicted.append(calibrated)
    return dict(origin=int(observation['step']),horizons=[4,8,24],directions=['net_sell','net_buy'],thresholds=[1,6],products=list(PRODUCTS),probabilities=coherent(predicted),raw_probabilities=coherent(raw),target='successful_sell_minus_successful_buy',boundary='Net successful trade units, including one-dollar sales. This is not actual inventory pressure or a production policy.')
