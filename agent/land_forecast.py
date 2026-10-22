"""Match the acquired land level to the opponent's actual starting level."""
import gzip,hashlib,json


def load_prior(directory,record):
    raw=(directory/record['file']).read_bytes()
    if hashlib.sha256(raw).hexdigest()!=record['sha256']:raise ValueError('Land prior hash mismatch')
    data=json.loads(gzip.decompress(raw))
    if len(data)!=22 or any(len(day)!=3 for day in data):raise ValueError('Invalid land prior dimensions')
    return data


def apply(bank,neighbors,weights,current,day,forecast,prior):
    land=int(current['land']);compatible=[(i,w) for i,w in zip(neighbors,weights) if bank[(i*30+day)*9+8]==land];total=sum(w for _,w in compatible)
    for hi,horizon in enumerate((1,3,7)):
        future=min(29,day+horizon);target=forecast['horizons'][str(horizon)]
        if compatible:
            distribution=[0.]*5
            for i,w in compatible:distribution[max(land,bank[(i*30+future)*9+8])]+=w/total
            support='same_current_land_neighbors'
        else:
            distribution=prior[day-6][hi][str(land)];support='training_absolute_land_prior_no_compatible_neighbor'
        probability=sum(distribution[land+1:]);mean=min(4,max(land,sum(i*w for i,w in enumerate(distribution))));quantiles={};cumulative=0.
        for value,w in enumerate(distribution):
            cumulative+=w
            for quantile in (.1,.5,.9):
                if quantile not in quantiles and cumulative>=quantile:quantiles[quantile]=value
        assert len(quantiles)==3 and 0<=probability<=1+1e-12 and land<=mean<=4+1e-12
        target['land_forecast']=dict(probability=min(1,probability),mean=mean,median=quantiles[.5],lower10=quantiles[.1],upper90=quantiles[.9],compatible_neighbors=len(compatible),support=support)
    forecast['land_projection_evidence']='land_forecast is conditioned on the current land level. Existing count vectors and raw_new_land_probability remain the original source-change diagnostics.'
