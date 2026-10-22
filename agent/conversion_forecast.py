"""Scores for observed same-tile replacement, without inferring hidden intent."""
import gzip,hashlib,json,struct

EVENTS=((6,7,'GOOSE_TO_COW'),(6,8,'GOOSE_TO_SHEEP'),(7,6,'COW_TO_GOOSE'),(7,8,'COW_TO_SHEEP'),(8,6,'SHEEP_TO_GOOSE'),(8,7,'SHEEP_TO_COW'),(0,6,'CROP_TO_GOOSE'),(0,7,'CROP_TO_COW'),(0,8,'CROP_TO_SHEEP'))


def read_checked(directory,record):
    raw=(directory/record['file']).read_bytes()
    if hashlib.sha256(raw).hexdigest()!=record['sha256']:raise ValueError('Conversion data hash mismatch')
    return gzip.decompress(raw)


def load_model(directory,record):
    data=read_checked(directory,record['targets']);magic,n,days,horizons,width=struct.unpack_from('<4s4I',data)
    if magic!=b'BC01' or (days,horizons,width)!=(22,3,9) or n!=record['samples'] or len(data)!=20+n*days*horizons*width:
        raise ValueError('Invalid conversion target bank')
    priors=json.loads(read_checked(directory,record['priors']))
    if record['method'] not in ('conditional_prior','raw_neighbors','compatible_neighbors','capacity_neighbors'):
        raise ValueError('Unsupported conversion model')
    calibration=json.loads(read_checked(directory,record['calibration']))
    if calibration['kind']!='mixture' or not 0<=calibration['weight']<=1:raise ValueError('Unsupported conversion calibration')
    return dict(targets=memoryview(data)[20:],priors=priors,method=record['method'],calibration=calibration)


def project(model,asset_bank,neighbors,weights,current,day):
    own=current['assets'];result={};eligible_sources={0:sum(own[:5]),6:own[5],7:own[6],8:own[7]}
    for hi,horizon in enumerate((1,3,7)):
        scores={}
        for ei,(source,destination,name) in enumerate(EVENTS):
            count=eligible_sources[source];probability=[];support=[];base=[]
            for ti,threshold in enumerate((1,2)):
                if count<threshold:probability.append(0.);support.append(0);base.append(0.);continue
                prior=model['priors'][day-6][hi][ti][ei]
                fallback=prior['counts'].get(str(count),prior['fallback']);base.append(fallback)
                if model['method']=='conditional_prior':probability.append(fallback);support.append(0);continue
                numerator=0.;denominator=0.;used=0
                for index,w in zip(neighbors,weights):
                    start=(index*30+day)*9
                    available=sum(asset_bank[start:start+5]) if source==0 else asset_bank[start+source-1]
                    if model['method']!='raw_neighbors' and available<threshold:continue
                    if model['method']=='capacity_neighbors':w/=1+((available-count)/4.)**2
                    converted=model['targets'][((index*22+day-6)*3+hi)*9+ei]
                    numerator+=w*int(converted>=threshold);denominator+=w;used+=1
                probability.append(min(1.,max(0.,numerator/denominator if denominator else fallback)));support.append(used)
            probability[1]=min(probability[0],probability[1])
            base[1]=min(base[0],base[1]);weight=model['calibration']['weight']
            calibrated=[weight*a+(1-weight)*b for a,b in zip(probability,base)]
            scores[name]=dict(source_tiles=count,at_least_one=calibrated[0],at_least_two=calibrated[1],raw_at_least_one=probability[0],raw_at_least_two=probability[1],prior_at_least_one=base[0],prior_at_least_two=base[1],compatible_neighbors=support)
        result[str(horizon)]=dict(day=min(29,day+horizon),events=scores)
    return dict(horizons=result,method=model['method'],boundary='Empirical scores for public dawn-endpoint occupants changing on the same original tile. No intent, exact conversion time, hidden inventory or production-decision override.')
