"""Calibrate signed resource quantities using public state and the existing cohort."""
import gzip
import hashlib
import json
import math


def load_model(directory,record):
    raw=(directory/record['file']).read_bytes()
    if hashlib.sha256(raw).hexdigest()!=record['sha256']:
        raise ValueError('Resource quantity calibration hash mismatch')
    model=json.loads(gzip.decompress(raw))
    if model['schema']!='public-resource-quantity-v1' or model['products']!=['WHEAT','FERTILIZER']:
        raise ValueError('Unsupported resource quantity model')
    coefficients=model['coefficient']
    if len(coefficients)!=3 or any(len(products)!=2 for products in coefficients):
        raise ValueError('Resource quantity dimensions differ')
    if any(len(row)!=12 or any(not math.isfinite(v) for v in row) for products in coefficients for row in products):
        raise ValueError('Invalid resource quantity coefficients')
    return model


def observation_key(public):
    return (public['cash'],public['ready'][0],sum(public['assets'][5:8]),
            sum(public['fertilizer'][5:8]),public['assets'][0],public['prices'][0],public['prices'][8])


def apply(model,quantity,rows,weights,public,day):
    if quantity is None:
        return
    if len(rows)!=8 or len(weights)!=8:
        raise ValueError('Resource quantity model requires the frozen eight-neighbor retrieval')
    before=quantity['point_estimate'];output=[list(row) for row in before]
    cash,ready_wheat,animals,fertilizer,wheat_assets,_,_=observation_key(public)
    for h,slots in enumerate((1,2,6)):
        for pi,p in enumerate((0,8)):
            amounts=[sum(row[9+slot*9+p] for slot in range(slots)) for row in rows]
            total=0.;median=sorted(amounts)[-1]
            for value,weight in sorted(zip(amounts,weights)):
                total+=weight
                if total>=.5:
                    median=value
                    break
            sell=sum(weight for value,weight in zip(amounts,weights) if value>=1)
            buy=sum(weight for value,weight in zip(amounts,weights) if value<=-1)
            mean=quantity['raw_neighbor_mean'][h][p]
            features=(1.,mean/6,median/6,sell,buy,math.log1p(public['prices'][p]/(25 if p==0 else 100)),
                      math.log1p(max(0.,cash)/1000),ready_wheat/6,animals/4,fertilizer/6,wheat_assets/4,day/30)
            output[h][p]=mean+6*sum(a*b for a,b in zip(features,model['coefficient'][h][pi]))
    quantity['point_estimate']=tuple(tuple(row) for row in output)
    quantity['uncalibrated_resource_net']=tuple((row[0],row[8]) for row in before)
    quantity['resource_boundary']='Signed net SELL minus BUY. Global robust calibration trained with paired source-excluded retrieval views; positive and negative estimates are allowed. This is not a stock bound or an executable order.'
