"""Weight frozen sale-probability experts using completed public evidence only."""
import hashlib
import json
import math

from goods_events import coherent

PRODUCTS=('CARROT','TOMATO','STRAWBERRY','MELON','EGG','MILK','WOOL')


def load_model(directory,record):
    raw=(directory/record['file']).read_bytes()
    if hashlib.sha256(raw).hexdigest()!=record['sha256']:
        raise ValueError('Adaptive goods-event model hash differs')
    model=json.loads(raw)
    if model['schema']!='public-adaptive-goods-events-v1' or model['prior']!=.9 or model['history']!=3 or model['rate']!=2.:
        raise ValueError('Unsupported adaptive goods-event parameters')
    return model


class AdaptiveGoodsEvents:
    def __init__(self,model):
        self.model=model
        self.reset()

    def reset(self):
        self.day=None
        self.losses=[]
        self.pending=None
        self.scored_cells=0

    def observe(self,observation):
        tick=int(observation['step']);day=tick//24
        if tick%24 or self.day==day:return
        if self.day is not None and day<self.day:
            raise ValueError('Reset event learning before a new game')
        flow=observation.get('_public_rival_cargo',{}).get('goods_flow')
        eligible=bool(flow and flow.get('origin')==tick and flow.get('started_at_zero'))
        windows=flow.get('completed_windows') if eligible else None
        if windows is not None and self.pending is not None and self.pending['day']==day-1:
            if tuple(flow['products'])!=PRODUCTS or len(windows)!=3 or any(len(row)!=7 for row in windows):
                raise ValueError('Adaptive events require certified seven-good4/8/24 prefixes')
            gains=[]
            for hi in range(3):
                row=[]
                for pi in range(7):
                    value=windows[hi][pi]
                    if value is None:
                        row.append(0.);continue
                    if not isinstance(value,int) or value<0:
                        raise ValueError('Certified sales require nonnegative integer quantities')
                    losses=[]
                    for field in ('old','new'):
                        loss=0.
                        for ti,threshold in enumerate((1,6)):
                            probability=max(.001,min(.999,self.pending[field][hi][ti][pi]))
                            loss-=math.log(probability if value>=threshold else 1-probability)/2
                        losses.append(loss)
                    row.append(losses[0]-losses[1]);self.scored_cells+=1
                gains.append(tuple(row))
            self.losses.append((day-1,tuple(gains)))
        self.losses=[(when,value) for when,value in self.losses if when>=day-self.model['history']]
        self.pending=None;self.day=day

    def weights(self,day):
        prior=math.log(self.model['prior']/(1-self.model['prior']))
        output=[]
        for hi in range(3):
            row=[]
            for pi in range(7):
                value=prior+self.model['rate']*sum(gain[hi][pi] for when,gain in self.losses if day-self.model['history']<=when<day)
                row.append(1/(1+math.exp(-max(-40.,min(40.,value)))))
            output.append(tuple(row))
        return tuple(output)

    def remember(self,head,day):
        if self.day!=day:
            raise ValueError('Observe the actual dawn before registering its event prediction')
        if head is not None and head.get('event_adaptive_status')=='applied':
            self.pending=dict(day=day,old=head['goods_event_original'],new=head['event_adaptive_original'])

    def state(self):
        return dict(day=self.day,losses=self.losses,pending=self.pending,scored_cells=self.scored_cells)


def apply(controller,head,day):
    if head is None:return
    if head.get('goods_event_status')!='applied':
        head['event_adaptive_status']='unavailable_public_event_base';return
    if head['horizons']!=[4,8,24] or head['target']!='successful_sell_minus_successful_buy':
        raise ValueError('Adaptive goods events require dawn net-sale probabilities')
    old=head['goods_event_original'];new=tuple(tuple(tuple(row[1:8]) for row in horizon[0]) for horizon in head['probabilities'])
    weights=controller.weights(day)
    mixed=coherent([[[ (1-weights[hi][pi])*old[hi][ti][pi]+weights[hi][pi]*new[hi][ti][pi]
        for pi in range(7)] for ti in range(2)] for hi in range(3)])
    output=[[list(map(list,direction)) for direction in horizon] for horizon in head['probabilities']]
    for hi in range(3):
        for ti in range(2):output[hi][0][ti][1:8]=mixed[hi][ti]
    head['probabilities']=tuple(tuple(tuple(tuple(row) for row in direction) for direction in horizon) for horizon in output)
    head['event_adaptive_original']=new;head['event_adaptive_weights']=weights;head['event_adaptive_status']='applied'
    head['event_adaptive_boundary']='Only completed identifiable public sales update previousactual-dawn expert losses. Latestthree days;unknown windows receive no label. Forecast queries do not train. Point quantities andplanner flows unchanged.'
