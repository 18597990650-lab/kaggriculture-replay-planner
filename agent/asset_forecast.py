"""Public asset-change projections reusing the supply model's eight neighbors."""
import gzip,hashlib,struct


def load_bank(directory,record):
    raw=(directory/record['file']).read_bytes()
    if hashlib.sha256(raw).hexdigest()!=record['sha256']:raise ValueError('Asset target hash mismatch')
    data=gzip.decompress(raw);magic,n,days,width=struct.unpack_from('<4s3I',data)
    if magic!=b'BA01' or days!=30 or width!=9 or n!=record['samples'] or len(data)!=16+n*days*width:raise ValueError('Invalid asset target bank')
    return memoryview(data)[16:]


def project(bank,neighbors,weights,current,day):
    """Median count forecast plus empirical intervals, not a legal action plan."""
    current_counts=list(current['assets'][:8])+[current['land']]
    result={}
    for horizon in (1,3,7):
        future=min(29,day+horizon)
        deltas=[[bank[(i*30+future)*9+p]-bank[(i*30+day)*9+p] for p in range(9)] for i in neighbors]
        mean=[];median=[];low=[];high=[];growth=[];exit_=[]
        for p,initial in enumerate(current_counts):
            values=sorted((row[p],w) for row,w in zip(deltas,weights));cumulative=0.;quantiles={}
            for value,w in values:
                cumulative+=w
                for probability in (.1,.5,.9):
                    if probability not in quantiles and cumulative>=probability:quantiles[probability]=value
            assert len(quantiles)==3
            bound=(lambda value:min(4,max(initial,value))) if p==8 else (lambda value:max(0,value))
            mean.append(bound(initial+sum(row[p]*w for row,w in zip(deltas,weights))))
            low.append(bound(initial+quantiles[.1]));median.append(bound(initial+quantiles[.5]));high.append(bound(initial+quantiles[.9]))
            if p<8:
                growth.append(sum(w for row,w in zip(deltas,weights) if row[p]>=2))
                exit_.append(sum(w for row,w in zip(deltas,weights) if row[p]<=-2))
        result[str(horizon)]=dict(day=future,median=median,mean=mean,lower10=low,upper90=high,
            raw_growth_probability=growth,raw_decline_probability=exit_,
            raw_new_land_probability=sum(w for row,w in zip(deltas,weights) if row[8]>0))
    return dict(horizons=result,columns=['WHEAT','CARROT','TOMATO','STRAWBERRY','MELON','EGG','MILK','WOOL','LAND'],
                boundary='Conditional asset counts and uncalibrated neighbor probabilities. Intervals have measured, not guaranteed, coverage. No private stock or executable action sequence.')
