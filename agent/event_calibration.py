"""Calibrate public count-growth probabilities without another neighbor search."""
import hashlib,json,math


def load_model(directory,record):
    raw=(directory/record['file']).read_bytes()
    if hashlib.sha256(raw).hexdigest()!=record['sha256']:raise ValueError('Event calibration hash mismatch')
    model=json.loads(raw)
    if model['schema']!='public-growth-calibration-v1' or model['columns']!=[0,1,2,3,4,5] or len(model['priors'])!=22:raise ValueError('Unsupported growth calibration')
    model['linear']=[a/b for a,b in zip(model['coefficient'],model['scale'])]
    model['offset']=-sum(a*b for a,b in zip(model['linear'],model['mean']))
    if not all(math.isfinite(v) for v in model['linear']+[model['offset']]):raise ValueError('Invalid calibration coefficients')
    return model


def logit(value):
    p=min(.999,max(.001,value));return math.log(p/(1-p))


def apply(model,day,distances,weights,forecast):
    minimum=math.log1p(min(distances));average=math.log1p(sum(d*w for d,w in zip(distances,weights)))
    for hi,horizon in enumerate((1,3,7)):
        target=forecast['horizons'][str(horizon)];values=[]
        for p,raw in enumerate(target['raw_growth_probability']):
            score=logit(raw);prior=logit(model['priors'][day-6][hi][p]);features=(1.,score,prior,minimum,average,(score-prior)*average)
            linear=model['offset']+sum(a*b for a,b in zip(model['linear'],features));values.append(1/(1+math.exp(-min(40,max(-40,linear)))))
        target['calibrated_growth_probability']=values
    forecast['growth_calibration_evidence']='Source-dropout development calibration, followed by held-submission checks. Only growth is calibrated; no guarantee for every strategy family.'
