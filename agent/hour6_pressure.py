"""Exact-window net trade probabilities at the existing hour6 query."""
import gzip,hashlib,json,math,struct
from pressure_forecast import PRODUCTS,BASE,coherent,logit


def load_model(directory,record,training_samples):
    raw=(directory/record['file']).read_bytes()
    if hashlib.sha256(raw).hexdigest()!=record['sha256']:raise ValueError('Hour6 model hash mismatch')
    model=json.loads(gzip.decompress(raw))
    if model['schema']!='public-hour6-pressure-runtime-v1' or len(model['linear'])!=21 or len(model['priors'])!=22:raise ValueError('Unsupported hour6 pressure model')
    if not all(math.isfinite(v) for v in model['linear']+[model['offset']]):raise ValueError('Invalid hour6 pressure coefficients')
    item=model['targets'];raw=(directory/item['file']).read_bytes()
    if hashlib.sha256(raw).hexdigest()!=item['sha256']:raise ValueError('Hour6 targets hash mismatch')
    raw=gzip.decompress(raw);magic,n,days,width=struct.unpack_from('<4s3I',raw)
    if magic!=b'HP06' or n!=training_samples or days!=22 or width!=27 or len(raw)!=16+n*days*width:raise ValueError('Invalid hour6 pressure targets')
    if max(raw[16:])>15:raise ValueError('Invalid hour6 event bitmask')
    model['bank']=raw[16:];return model


def observation_key(observation):
    if int(observation['step'])%24!=6:return None
    cargo=observation.get('_public_rival_cargo')
    cargo=cargo.get('own_sale_corrected') if cargo is not None else None
    if cargo is None or not cargo['started_at_zero'] or cargo['origin']!=int(observation['step']):return None
    return tuple(cargo['quantities'].get(p,0) for p in PRODUCTS)+tuple(cargo['uncertain_upper'].get(p,0) for p in PRODUCTS)


def project(model,neighbors,weights,distances,observation,current,day):
    cargo=observation_key(observation)
    if cargo is None:return None
    minimum=math.log1p(min(distances));average=math.log1p(sum(d*w for d,w in zip(distances,weights)))
    known=[math.log1p(v/6) for v in cargo[:9]];upper=[math.log1p(v/6) for v in cargo[9:]];price=[math.log1p(observation['market']['prices'][p]/b) for p,b in zip(PRODUCTS,BASE)]
    assets=list(current['assets']);assets[-1]=sum(assets[5:8]);ready=list(current['ready']);ready[-1]=sum(current['fertilizer'][5:8]);due=list(current['phase'][:9]);due[-1]=0
    assets=[math.log1p(v/4) for v in assets];ready=[math.log1p(v/6) for v in ready];due=[math.log1p(v/4) for v in due];cash=math.log1p(max(0,current['cash'])/1000)
    rows=[model['bank'][i*22*27+(day-6)*27:i*22*27+(day-5)*27] for i in neighbors];raw=[];predicted=[]
    for hi in range(3):
        source=[];calibrated=[]
        for di,direction in enumerate((1.,-1.)):
            source_direction=[];target_direction=[]
            for ti in range(2):
                mask=1<<(di*2+ti);probabilities=[];values=[]
                for p in range(9):
                    probability=sum(w for row,w in zip(rows,weights) if row[hi*9+p]&mask);probabilities.append(probability)
                    score=logit(probability);prior=logit(model['priors'][day-6][hi][di][ti][p]);features=(1.,score,prior,minimum,average,(score-prior)*average,known[p],upper[p],price[p],direction,known[p]*direction,upper[p]*direction,price[p]*direction,assets[p],ready[p],due[p],assets[p]*direction,ready[p]*direction,due[p]*direction,cash,cash*direction)
                    value=model['offset']+sum(a*b for a,b in zip(model['linear'],features));values.append(1/(1+math.exp(-min(40,max(-40,value)))))
                source_direction.append(probabilities);target_direction.append(values)
            source.append(source_direction);calibrated.append(target_direction)
        raw.append(source);predicted.append(calibrated)
    return dict(origin=int(observation['step']),horizons=[4,8,24],directions=['net_sell','net_buy'],thresholds=[1,6],products=list(PRODUCTS),probabilities=coherent(predicted),raw_probabilities=coherent(raw),target='successful_sell_minus_successful_buy',boundary='Net successful trade units, including one-dollar sales. This is not actual inventory pressure or a production policy.')
