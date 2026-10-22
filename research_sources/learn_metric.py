"""Train small linear retrieval embeddings on whole-episode-separated trajectories."""
import os
os.environ['OPENBLAS_NUM_THREADS']='1'
os.environ['OMP_NUM_THREADS']='1'
import hashlib
import json
from pathlib import Path
import sys
import time
import numpy as np

HERE=Path(__file__).resolve().parent
PARENT=HERE.parent
sys.path.insert(0,str(PARENT/'iteration02'))
sys.path.insert(1,str(PARENT))
from prepare import load,save
from evaluate_phase import configure
import evaluate_forecast as core
from public_features import add_history


def matrix(rows,day,keys):
    records=[add_history(r['public'],day) for r in rows]
    blocks=[np.array([core.vector(r,key) for r in records]) for key in keys]
    return np.concatenate(blocks,axis=1)


def phase(day):
    return 0 if day<12 else 1 if day<21 else 2


def main():
    assert not (HERE/'METRIC_DEV.json').exists()
    configure()
    data=core.load(PARENT/'iteration02/DATASET.json')['samples']
    train=[r for r in data if r['source']['split']=='train']
    dev=[r for r in data if r['source']['split']=='dev']
    assert not {r['source']['world_seed'] for r in train}&{r['source']['world_seed'] for r in dev}
    reference=load(PARENT/'iteration02/SELECTED.json')['parameters']
    keys=core.GROUPS
    train_x={d:matrix(train,d,keys) for d in range(6,28)}
    dev_x={d:matrix(dev,d,keys) for d in range(6,28)}
    values=np.array([r['net_sales'] for r in train],dtype=float)
    targets=np.array([r['net_sales'] for r in dev],dtype=float)
    assets=np.array([[f['assets'] for f in r['public']] for r in train],dtype=float)
    target_assets=np.array([[f['assets'] for f in r['public']] for r in dev],dtype=float)
    full_value=np.concatenate((values,assets),axis=2)
    models={}
    started=time.perf_counter()
    for penalty in (.01,.1):
        models[str(penalty)]={}
        for stage in (0,1,2):
            days=[d for d in range(6,28) if phase(d)==stage]
            x=np.concatenate([train_x[d] for d in days])
            y=np.concatenate([np.concatenate([values[:,d+1:min(30,d+1+h)].mean(axis=1) for h in (1,3,7)],axis=1) for d in days])
            mean=x.mean(axis=0);scale=np.maximum(x.std(axis=0),.25)
            ymean=y.mean(axis=0);yscale=np.maximum(y.std(axis=0),1.)
            z=(x-mean)/scale
            coefficient=np.linalg.solve(z.T@z+np.eye(z.shape[1])*len(z)*penalty,z.T@((y-ymean)/yscale))
            # Omitting the shared affine offset leaves Euclidean distances identical.
            raw_coefficient=coefficient/scale[:,None]
            models[str(penalty)][str(stage)]=dict(coefficient=raw_coefficient.tolist(),
                training_rows=len(x),ridge_penalty=penalty,output_dimensions=27)
        print(json.dumps(dict(fit_penalty=penalty,seconds=time.perf_counter()-started)),flush=True)
    specifications={'phase_raw':dict(reference=True)}
    for penalty in (.01,.1):
        for raw in ('none','coarse','full'):
            specifications[f'ridge{penalty}_{raw}']=dict(penalty=str(penalty),raw=raw,raw_weight=.25)
    names=list(specifications)
    scores=np.zeros((len(names),len(dev),22,3,4))
    dimensions={}
    slices={};offset=0
    for key in keys:
        width=len(core.vector(add_history(train[0]['public'],6),key))
        slices[key]=slice(offset,offset+width);offset+=width
    factors=np.zeros(offset)
    for key,weight in reference['weights'].items():factors[slices[key]]=weight**.5
    coarse=np.zeros(offset);coarse[slices['assets']]=1;coarse[slices['shops']]=1
    for day in range(6,28):
        raw_train=train_x[day];raw_dev=dev_x[day]
        price=np.array([r['prices'][day] for r in dev])
        for index,name in enumerate(names):
            spec=specifications[name]
            if spec.get('reference'):
                a,b=raw_dev*factors,raw_train*factors
                dimensions[name]=int(np.count_nonzero(factors))
            else:
                c=np.array(models[spec['penalty']][str(phase(day))]['coefficient'])
                a,b=raw_dev@c,raw_train@c
                if spec['raw']!='none':
                    mask=factors if spec['raw']=='full' else coarse
                    chosen=mask>0;weights=mask[chosen]*spec['raw_weight']**.5
                    a=np.concatenate((a,raw_dev[:,chosen]*weights),axis=1)
                    b=np.concatenate((b,raw_train[:,chosen]*weights),axis=1)
                dimensions[name]=a.shape[1]
            distances=core.squared_distance(a,b)
            prediction,_=core.predict(distances,full_value,8)
            for hi,h in enumerate((1,3,7)):
                scores[index,:,day-6,hi]=core.measure(prediction,targets,target_assets,price,day,h)
        if day%4==0:print(json.dumps(dict(day=day,seconds=time.perf_counter()-started)),flush=True)
    fields=['quantity_mae','price_weighted_mae','cumulative_quantity_mae','future_asset_count_mae']
    summaries={name:{str(h):dict(zip(fields,scores[i,:,:,hi].mean(axis=(0,1)).tolist())) for hi,h in enumerate((1,3,7))} for i,name in enumerate(names)}
    best=min(names,key=lambda name:summaries[name]['3']['price_weighted_mae'])
    result=dict(training_trajectories=len(train),development_trajectories=len(dev),world_overlap=0,
        keys=keys,feature_scales=core.SCALES,specifications=specifications,dimensions=dimensions,
        selected=best,summary=summaries,seconds=time.perf_counter()-started,
        models=models,source_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        boundary='Fit only on training trajectories. Twenty-seven mean-supply targets summarize 1/3/7-day horizons. Three time-of-season regressions define distances, never override our plan choice. No test results used for this selection.')
    save(HERE/'METRIC_DEV.json',result)
    np.savez_compressed(HERE/'METRIC_DEV_DETAIL.npz',errors=scores,versions=np.array(names),
                        sample_ids=np.array([r['source']['sample_id'] for r in dev]))
    print(json.dumps(dict(selected=best,summary=summaries,dimensions=dimensions)),flush=True)


if __name__=='__main__':main()
