"""Freeze the learned metric without modifying forecast targets or quote budgets."""
import gzip
import hashlib
import json
from pathlib import Path
import shutil
import struct
import sys
import numpy as np

from learn_metric import configure,load,save,core,matrix,phase,PARENT
HERE=Path(__file__).resolve().parent


def hashes(directory):
    return {p.relative_to(directory).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(directory.rglob('*')) if p.is_file() and '__pycache__' not in p.parts}


def main():
    configure()
    experiment=load(HERE/'METRIC_DEV.json');selected=experiment['selected']
    test=load(HERE/'METRIC_TEST_REGRESSION.json')
    assert test['summary'][selected]['3']['price_weighted_mae']<test['summary']['phase_raw']['3']['price_weighted_mae']
    spec=experiment['specifications'][selected]
    assert spec['raw']=='full', 'This adapter preserves the complete route descriptor'
    source=PARENT/'iteration03/candidate';target=HERE/'candidate'
    assert not target.exists()
    shutil.copytree(source,target,ignore=shutil.ignore_patterns('behavior_model','__pycache__','*.pyc'))
    shutil.copyfile(HERE/'metric_features.py',target/'metric_features.py')
    path=target/'behavior_predictor.py';code=path.read_text(encoding='utf-8')
    code=code.replace('from timing_tools import market_batches','from timing_tools import market_batches\nfrom metric_features import embedding')
    code=code.replace('public-behavior-timing-v1','public-behavior-metric-v1')
    code=code.replace("feature = flatten(current, self.manifest['parameters']['weights'])",'feature = embedding(current, day, self.manifest, flatten)')
    code=code.replace('/public_behavior_arrival_35','/public_behavior_learned_metric_35')
    compile(code,str(path),'exec');path.write_text(code,encoding='utf-8')
    data=core.load(PARENT/'iteration02/DATASET.json')['samples']
    training=[r for r in data if r['source']['split']=='train']
    model=load(source/'behavior_model/MODEL.json')
    from behavior_predictor import flatten
    import behavior_predictor
    from evaluate_phase import EXTRA_GROUPS,EXTRA_SCALES
    behavior_predictor.GROUPS=(*behavior_predictor.GROUPS,*EXTRA_GROUPS)
    behavior_predictor.SCALES.update(EXTRA_SCALES)
    output=target/'behavior_model';output.mkdir();days={}
    for day,record in model['days'].items():
        day=int(day);payload=gzip.decompress((source/'behavior_model'/record['file']).read_bytes())
        magic,d,n,old_width,horizon=struct.unpack_from('<4s4I',payload)
        assert magic==b'BHT1' and d==day and n==len(training)
        x=matrix(training,day,experiment['keys'])
        coefficient=np.array(experiment['models'][spec['penalty']][str(phase(day))]['coefficient'])
        projected=x@coefficient
        details=np.array([flatten(core.add_history(r['public'],day),model['parameters']['weights']) for r in training])*spec['raw_weight']**.5
        features=np.concatenate((projected,details),axis=1)
        width=features.shape[1]
        labels=payload[20+n*old_width*8:]
        raw=struct.pack('<4s4I',b'BHT1',day,n,width,horizon)+features.astype('<f8').tobytes()+labels
        packed=gzip.compress(raw,compresslevel=6,mtime=0)
        file=output/f'day-{day:02d}.bin.gz';file.write_bytes(packed)
        assert gzip.decompress(packed)[20+n*width*8:]==labels
        days[str(day)]=dict(file=file.name,bytes=len(packed),sha256=hashlib.sha256(packed).hexdigest())
    model.update(schema='public-behavior-metric-v1',name=selected,days=days,
        projection_groups=experiment['keys'],raw_metric_weight=spec['raw_weight'],
        projections={stage:np.array(record['coefficient']).T.tolist() for stage,record in experiment['models'][spec['penalty']].items()},
        selected_on='Grouped training regression fit and development-only model choice; repeated regression audit passed',
        metric_only=True,daily_forecast_unchanged=False,target_labels_unchanged=True)
    save(output/'MODEL.json',model)
    previous=load(PARENT/'iteration03/FROZEN_SOURCES.json')
    assert hashes(source)==previous['behavior03']['files']
    save(HERE/'FROZEN_SOURCES.json',dict(baseline=previous['behavior03'],
                                       behavior04=dict(directory=str(target),files=hashes(target))))
    print(json.dumps(dict(dimensions=width,compressed_bytes=sum(d['bytes'] for d in days.values()),
                         target_labels_unchanged=True,selected=selected)),flush=True)


if __name__=='__main__':main()
