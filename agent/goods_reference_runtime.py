"""Mix supplementary sale priors while preserving current stock and calibration."""
import math


def apply(model,calibration,quantity,original_rows,original_neighbors,selected,day):
    if quantity is None:
        return
    if selected is None:
        quantity['goods_reference_status']='unavailable_supplementary_match'
        return
    neighbors,weights=selected
    if len(neighbors)!=8 or len(weights)!=8:
        raise ValueError('Goods reference projection requires eight neighbors')
    base_count=model['base_training_count']
    lookup=dict(zip(original_neighbors,original_rows))
    additions=model['days'][str(day)]['targets']
    rows=[lookup[index] if index<base_count else additions[index-base_count] for index in neighbors]
    original=quantity['point_estimate']
    output=[list(row) for row in original]
    expanded=[[0.]*7 for _ in range(3)]
    membership=quantity['route_probabilities']
    if len(membership)!=6 or model['goods_reference_weight']!=.25:
        raise ValueError('Goods mixture calibration contract differs')
    for hi,slots in enumerate((1,2,6)):
        for product in range(1,8):
            values=sorted((sum(row[9+slot*9+product] for slot in range(slots)),weight) for row,weight in zip(rows,weights))
            mean=sum(value*weight for value,weight in values)
            cumulative=0.
            for median,weight in values:
                cumulative+=weight
                if cumulative>=.5:
                    break
            else:
                raise ValueError('Invalid supplementary weights')
            capacity=quantity['estimated_goods_capacity'][product-1]
            raw=max(0.,min(capacity,mean))
            base=raw/6
            present=float(capacity>0)
            features=(base,min(capacity,median)/6,capacity/6,present,
                      *(w*base for w in membership),*(w*present for w in membership))
            correction=6*sum(a*b for a,b in zip(features,calibration['coefficient'][hi][product-1]))
            value=max(0.,min(capacity,raw+correction))
            expanded[hi][product-1]=max(value,expanded[hi-1][product-1]) if hi else value
    for hi in range(3):
        for product in range(1,8):
            expanded[hi][product-1]=min(expanded[hi][product-1],quantity['estimated_mature_goods_capacity'][product-1])
            value=.75*original[hi][product]+.25*expanded[hi][product-1]
            if not math.isfinite(value):
                raise ValueError('Nonfinite supplementary goods forecast')
            output[hi][product]=value
    quantity['point_estimate']=tuple(tuple(row) for row in output)
    quantity['goods_reference_status']='applied'
    quantity['goods_reference_experts']=dict(old=tuple(tuple(row[1:8]) for row in original),new=tuple(tuple(row) for row in expanded))
    quantity['goods_reference_weight']=.25
    quantity['goods_reference_boundary']='Seven non-purchasable net-sale quantities. Current public stock,capacity,original route memberships and calibration coefficients remain fixed. One-dollar sales are included;this is not market inventory pressure.'
