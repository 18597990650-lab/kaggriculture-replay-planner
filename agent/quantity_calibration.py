"""Cheap route-weighted calibration of cumulative net trade quantities."""
import gzip
import hashlib
import json
import math


def load_model(directory,record,training_samples,training_order_sha256):
    raw=(directory/record['file']).read_bytes()
    if hashlib.sha256(raw).hexdigest()!=record['sha256']:
        raise ValueError('Quantity calibration hash mismatch')
    model=json.loads(gzip.decompress(raw))
    if model['schema']!='public-route-quantity-v1':
        raise ValueError('Unsupported quantity calibration schema')
    if model['training_order_sha256']!=training_order_sha256:
        raise ValueError('Quantity cohorts and retrieval rows use different training orders')
    groups=model['assignments'];coefficients=model['coefficient']
    if len(groups)!=training_samples or any(not isinstance(g,int) or not 0<=g<6 for g in groups):
        raise ValueError('Invalid quantity cohort assignments')
    if len(coefficients)!=3 or any(len(products)!=7 for products in coefficients):
        raise ValueError('Quantity calibration horizon or product dimensions differ')
    if any(len(row)!=16 or any(not math.isfinite(v) for v in row) for products in coefficients for row in products):
        raise ValueError('Invalid quantity calibration coefficients')
    return model


def apply(model,quantity,rows,weights,neighbors):
    if quantity is None:
        return
    if not len(rows)==len(weights)==len(neighbors)==8:
        raise ValueError('Quantity calibration expects the frozen eight-neighbor retrieval')
    memberships=[0.]*6
    for index,weight in zip(neighbors,weights):
        memberships[model['assignments'][index]]+=weight
    original=quantity['point_estimate']
    output=[list(row) for row in original]
    for h,slots in enumerate((1,2,6)):
        for p in range(1,8):
            values=sorted((sum(row[9+slot*9+p] for slot in range(slots)),weight)
                          for row,weight in zip(rows,weights))
            accumulated=0.
            median=values[-1][0]
            for value,weight in values:
                accumulated+=weight
                if accumulated>=.5:
                    median=value
                    break
            capacity=quantity['estimated_goods_capacity'][p-1]
            base=original[h][p]/6
            present=float(capacity>0)
            features=(base,min(capacity,median)/6,capacity/6,present,
                      *(w*base for w in memberships),*(w*present for w in memberships))
            correction=6*sum(a*b for a,b in zip(features,model['coefficient'][h][p-1]))
            value=max(0.,min(capacity,original[h][p]+correction))
            output[h][p]=max(value,output[h-1][p]) if h else value
    quantity['uncalibrated_capacity_point']=original
    quantity['point_estimate']=tuple(tuple(row) for row in output)
    quantity['route_probabilities']=tuple(memberships)
    quantity['calibration']='Robust per-product/horizon residual, soft six-calendar membership, route coefficients shrunk toward the global formula.'
