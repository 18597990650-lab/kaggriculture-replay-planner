"""Estimate ambiguous carried stock from an already selected public-state cohort."""
import gzip
import hashlib
import struct

PRODUCTS=('CARROT','TOMATO','STRAWBERRY','MELON','EGG','MILK','WOOL')


def load_bank(directory, record, training_samples):
    raw=(directory/record['file']).read_bytes()
    if hashlib.sha256(raw).hexdigest()!=record['sha256']:
        raise ValueError('Historical stock label bank hash mismatch')
    payload=gzip.decompress(raw)
    magic,count,days,width=struct.unpack_from('<4s3I',payload)
    if (magic,count,days,width)!=(b'ST01',training_samples,30,len(PRODUCTS)):
        raise ValueError('Historical stock bank dimensions do not match the public retrieval model')
    if len(payload)!=16+2*count*days*width:
        raise ValueError('Truncated historical stock bank')
    return struct.unpack_from(f'<{count*days*width}H',payload,16)


def observation_key(observation):
    step=int(observation['step'])
    if step%24:
        return None
    cargo=observation.get('_public_rival_cargo',{}).get('cash_floor_corrected')
    if not cargo or cargo.get('origin')!=step or not cargo.get('started_at_zero'):
        return None
    known=tuple(cargo['quantities'].get(p,0) for p in PRODUCTS)
    uncertain=tuple(cargo['uncertain_upper'].get(p,0) for p in PRODUCTS)
    return known,uncertain


def project(bank, neighbors, weights, observation, day):
    key=observation_key(observation)
    if key is None:
        return None
    known,uncertain=key
    accounting=tuple(a+b for a,b in zip(known,uncertain))
    point=list(accounting)
    medians=[]
    for pi in range(len(PRODUCTS)):
        values=sorted((bank[(index*30+day)*len(PRODUCTS)+pi],weight)
                      for index,weight in zip(neighbors,weights))
        total=0.
        median=values[-1][0]
        for value,weight in values:
            total+=weight
            if total>=.5:
                median=value
                break
        medians.append(median)
        if uncertain[pi]>0:
            point[pi]=min(accounting[pi],median)
    return dict(products=PRODUCTS,point_estimate=tuple(point),
        public_accounting_estimate=accounting,uncertain_products=tuple(p for p,n in zip(PRODUCTS,uncertain) if n>0),
        neighbor_median=tuple(medians),origin=int(observation['step']),
        evidence='Dawn-only posterior from public-state neighbors and public stock accounting. Historical private stock is a training target; current private rival stock is unknown. Accounting estimates are not guaranteed upper bounds.')
