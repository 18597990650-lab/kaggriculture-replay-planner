"""Use publicly identifiable completed days to weight wheat-sale forecasts."""
import gzip
import hashlib
import json
import math
from pressure_forecast import logit
from relative_grain import observation_key


def load_model(directory, record):
    packed=(directory/record['file']).read_bytes()
    if hashlib.sha256(packed).hexdigest()!=record['sha256']:
        raise ValueError('Adaptive sale model hash mismatch')
    model=json.loads(gzip.decompress(packed))
    if model['schema']!='public-adaptive-wheat-sale-v1' or len(model['linear'])!=3 or len(model['offset'])!=3:
        raise ValueError('Adaptive sale model shape mismatch')
    if any(len(row)!=23 or not all(math.isfinite(value) for value in row) for row in model['linear']):
        raise ValueError('Invalid adaptive sale coefficients')
    if not all(math.isfinite(value) for value in model['offset']):
        raise ValueError('Invalid adaptive sale offsets')
    if not 0<model['prior']<1 or model['history_days']!=3 or model['learning_rate']!=1.:
        raise ValueError('Unsupported adaptive sale update contract')
    return model


class AdaptiveSale:
    def __init__(self, model):
        self.model=model
        self.reset()

    def reset(self):
        self.day=None
        self.losses=[]
        self.pending=None
        self.updates=0

    def observe(self, observation):
        tick=int(observation['step'])
        day=tick//24
        if tick%24 or self.day==day:
            return
        if self.day is not None and day<self.day:
            raise ValueError('Reset adaptive sales before starting another game')
        flow=observation.get('_public_rival_cargo',{}).get('resource_flow')
        eligible=bool(flow and flow.get('origin')==tick and flow.get('started_at_zero')
                      and flow.get('window_steps')==24 and flow['observed_steps_last24'][0]==24)
        windows=flow.get('completed_wheat_windows') if eligible else None
        if self.pending is not None and self.pending['day']==day-1 and windows is not None:
            if len(windows)!=3:
                raise ValueError('Adaptive sales require4/8/24 completed prefixes')
            gains=[]
            for hi, net in enumerate(windows):
                losses=[]
                for field in ('old','new'):
                    loss=0.
                    for ti, threshold in enumerate((1,6)):
                        probability=min(.999,max(.001,self.pending[field][hi][ti]))
                        loss-=math.log(probability if net>=threshold else 1-probability)/2
                    losses.append(loss)
                gains.append(losses[0]-losses[1])
            self.losses.append((day-1,tuple(gains)))
            self.updates+=1
        self.losses=[(when,value) for when,value in self.losses if when>=day-self.model['history_days']]
        self.pending=None
        self.day=day

    def weights(self, day):
        prior=math.log(self.model['prior']/(1-self.model['prior']))
        result=[]
        for hi in range(3):
            value=prior+self.model['learning_rate']*sum(gain[hi] for when,gain in self.losses if day-self.model['history_days']<=when<day)
            result.append(1/(1+math.exp(-min(40.,max(-40.,value)))))
        return tuple(result)

    def remember(self, head, day):
        if self.day!=day:
            raise ValueError('Observe the actual dawn before registering its prediction')
        if head is not None and head.get('wheat_sale_status')=='applied':
            pair=head['wheat_sale_experts']
            self.pending=dict(day=day,old=tuple(tuple(row) for row in pair['old']),new=tuple(tuple(row) for row in pair['new']))

    def state(self):
        return dict(day=self.day,losses=self.losses,pending=self.pending,updates=self.updates)


def apply(controller, head, quantity, pressure, rows, weights, distances, public, observation, day):
    if head is None or int(observation['step'])%24:
        return
    key=observation_key(observation)
    if key is None or quantity is None or quantity.get('relative_grain_status')!='applied':
        head['wheat_sale_status']='unavailable_public_grain'
        return
    if head['horizons']!=[4,8,24] or head['target']!='successful_sell_minus_successful_buy':
        raise ValueError('Adaptive sales require dawn net-trade forecasts')
    grain,unknown,ambiguous,net24,count24,net8,count8=key
    reference=quantity['public_grain_estimate']
    animals=sum(public['assets'][5:8])
    extra=(net24/12,count24/24,net8/6,count8/8,
           math.log1p(grain/6)-math.log1p(reference['reference_median']/6),
           (max(0.,animals-grain)-reference['reference_feeding_shortfall'])/6,
           math.log1p(unknown),math.log1p(ambiguous),math.log1p(max(0.,public['cash'])/1000),
           public['ready'][0]/6,animals/4,public['assets'][0]/4,day/30)
    minimum=math.log1p(min(distances))
    average=math.log1p(sum(distance*weight for distance,weight in zip(distances,weights)))
    mixing=controller.weights(day)
    old=[];new=[]
    for hi,slots in enumerate((1,2,6)):
        amounts=[row[0] if slots==6 else sum(row[9+slot*9] for slot in range(slots)) for row in rows]
        original=tuple(head['probabilities'][hi][0][ti][0] for ti in range(2))
        values=[]
        for ti,threshold in enumerate((1,6)):
            probability=sum(weight for amount,weight in zip(amounts,weights) if amount>=threshold)
            score=logit(probability)
            prior=logit(pressure['priors'][day-6][hi][0][ti][0])
            features=(1.,score,prior,minimum,average,(score-prior)*average,0.,0.,math.log1p(public['prices'][0]/25),1.)+extra
            value=controller.model['offset'][hi]+sum(a*b for a,b in zip(controller.model['linear'][hi],features))
            values.append(1/(1+math.exp(-min(40.,max(-40.,value)))))
        values[0]=min(values[0],1-head['probabilities'][hi][1][0][0])
        values[1]=min(values[1],values[0])
        for ti in range(2):
            head['probabilities'][hi][0][ti][0]=(1-mixing[hi])*original[ti]+mixing[hi]*values[ti]
        old.append(original);new.append(tuple(values))
    head['wheat_sale_status']='applied'
    head['wheat_sale_experts']=dict(old=tuple(old),new=tuple(new))
    head['wheat_sale_weights']=mixing
    head['wheat_sale_boundary']='Weights use only identifiable completed prior days. Forecast queries do not update learning; actual dawn observations register predictions.'
