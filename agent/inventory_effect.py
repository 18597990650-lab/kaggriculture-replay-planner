"""Forecast actual inventory contributions separately from gross net trading behavior."""
import gzip,hashlib,json,math,struct
from pressure_forecast import PRODUCTS,BASE,coherent,logit,observation_key,stock_features


def load_model(directory,record,training_samples):
    raw=(directory/record['file']).read_bytes()
    if hashlib.sha256(raw).hexdigest()!=record['sha256']:raise ValueError('Inventory-effect model hash mismatch')
    model=json.loads(gzip.decompress(raw))
    if model['schema']!='public-inventory-effect-stock-v2' or len(model['linear'])!=12 or len(model['priors'])!=22:raise ValueError('Unsupported inventory-effect model')
    if not all(math.isfinite(v) for v in model['linear']+[model['offset']]):raise ValueError('Invalid inventory-effect coefficients')
    item=model['targets'];raw=(directory/item['file']).read_bytes()
    if hashlib.sha256(raw).hexdigest()!=item['sha256']:raise ValueError('Inventory-effect targets hash mismatch')
    raw=gzip.decompress(raw);magic,n,days,width=struct.unpack_from('<4s3I',raw)
    if magic!=b'IE00' or n!=training_samples or days!=22 or width!=27 or len(raw)!=16+n*days*width:raise ValueError('Invalid inventory-effect targets')
    if max(raw[16:])>15:raise ValueError('Invalid inventory-effect bitmask')
    model['bank']=raw[16:];return model


def project(model,neighbors,weights,distances,observation,day,stock):
    cargo=observation_key(observation)
    if cargo is None or stock is None:return None
    extra=stock_features(cargo,stock)
    minimum=math.log1p(min(distances));average=math.log1p(sum(d*w for d,w in zip(distances,weights)))
    known=[math.log1p(v/6) for v in cargo[:9]];upper=[math.log1p(v/6) for v in cargo[9:]];price=[math.log1p(observation['market']['prices'][p]/b) for p,b in zip(PRODUCTS,BASE)]
    rows=[model['bank'][i*22*27+(day-6)*27:i*22*27+(day-5)*27] for i in neighbors];raw=[];predicted=[]
    for hi in range(3):
        source=[];calibrated=[]
        for di,direction in enumerate((1.,-1.)):
            source_direction=[];target_direction=[]
            for ti in range(2):
                mask=1<<(di*2+ti);probabilities=[];values=[]
                for p in range(9):
                    probability=sum(w for row,w in zip(rows,weights) if row[hi*9+p]&mask);probabilities.append(probability)
                    score=logit(probability);prior=logit(model['priors'][day-6][hi][di][ti][p]);features=(1.,score,prior,minimum,average,(score-prior)*average,known[p],upper[p],price[p],direction,*extra[p])
                    value=model['offset']+sum(a*b for a,b in zip(model['linear'],features));values.append(1/(1+math.exp(-min(40,max(-40,value)))))
                source_direction.append(probabilities);target_direction.append(values)
            source.append(source_direction);calibrated.append(target_direction)
        raw.append(source);predicted.append(calibrated)
    return dict(origin=int(observation['step']),horizons=[4,8,24],directions=['inventory_increase','inventory_decrease'],thresholds=[1,6],products=list(PRODUCTS),probabilities=coherent(predicted),raw_probabilities=coherent(raw),target='rival_market_inventory_contribution',boundary='Official per-player inventory delta before town consumption. Floor-price sales excluded. Conditional on observed state; not a counterfactual trade schedule.')
