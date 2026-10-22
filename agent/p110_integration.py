"""Map calibrated dawn quantities to the existing native market simulator."""
from dataclasses import replace
import hashlib
import json
import math
import time

from behavior_predictor import Predictor
from day_sale_remainder import DaySales

PRODUCTS=('WHEAT','CARROT','TOMATO','STRAWBERRY','MELON','EGG','MILK','WOOL','FERTILIZER')
GOODS=PRODUCTS[1:8]
HORIZONS=(4,8,24)
HOURS=(2,6,10,14,18,22)


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def cumulative(flow,start):
    return [[-sum(batch.get(p,0) for tick,batch in flow.items() if start<=tick<start+h)
             for p in GOODS] for h in HORIZONS]


def apportion(total,weights):
    if any(not math.isfinite(v) or v<0 for v in weights):
        raise ValueError('P110 timing weights must be finite and nonnegative')
    if not sum(weights):weights=[1.]*len(weights)
    values=[total*v/sum(weights) for v in weights]
    result=[math.floor(v) for v in values]
    remaining=total-sum(result)
    for i in sorted(range(len(values)),key=lambda i:(-(values[i]-result[i]),i))[:remaining]:
        result[i]+=1
    assert sum(result)==total
    return result


def overlay(worlds,forecast,start):
    """Replace today's seven sale-only goods; leave all other assumptions intact."""
    record=dict(step=start,mode='q7',active=False,fallback_reason=None)
    if start%24 or not 6<=start//24<28:
        record['fallback_reason']='outside_valid_dawn'
        return worlds,record
    if forecast is None or forecast.get('net_trade_quantity') is None:
        record['fallback_reason']='missing_public_forecast'
        return worlds,record
    head=forecast['net_trade_quantity']
    if head.get('origin')!=start or tuple(head['products'])!=PRODUCTS or tuple(head['horizons'])!=HORIZONS:
        raise ValueError('P110 quantity origin or dimension contract mismatch')
    if head.get('compact_goods_status')!='applied':
        record['fallback_reason']=head.get('compact_goods_status','missing_calibration_status')
        return worlds,record
    values=head['point_estimate']
    if len(values)!=3 or any(len(row)!=9 for row in values):
        raise ValueError('P110 quantity dimensions differ')
    points=[[float(row[i]) for i in range(1,8)] for row in values]
    for i in range(7):
        q=[row[i] for row in points]
        if not all(math.isfinite(v) for v in q) or not 0<=q[0]<=q[1]<=q[2]:
            raise ValueError(('P110 nonmonotone or invalid sale quantity',GOODS[i],q))
    targets=[[math.floor(v+.5) for v in row] for row in points]
    slots=forecast['current_slots']
    if len(slots)!=6 or any(len(row)!=9 for row in slots):
        raise ValueError('P110 timing slots dimensions differ')
    batches=[{} for _ in HOURS];uniform=[]
    for pi,p in enumerate(GOODS):
        weights=[float(slots[j][pi+1]) for j in range(2,6)]
        if not all(math.isfinite(v) for v in weights):raise ValueError('Nonfinite P110 slots')
        weights=[max(0.,v) for v in weights]
        if not sum(weights):uniform.append(p)
        quantities=[targets[0][pi],targets[1][pi]-targets[0][pi],
                    *apportion(targets[2][pi]-targets[1][pi],weights)]
        for batch,q in zip(batches,quantities):
            if q:batch[p]=-q
    result=[];checks=[]
    for world in worlds:
        if world.scale!=1:
            raise ValueError('P110-Q7 requires unscaled market quantities')
        flow={tick:dict(batch) for tick,batch in world.external_flow.items()}
        for tick in list(flow):
            if start<=tick<start+24:
                for p in GOODS:flow[tick].pop(p,None)
                if not flow[tick]:del flow[tick]
        for hour,batch in zip(HOURS,batches):
            tick=start+hour
            if batch:
                assert start<=tick<719
                flow.setdefault(tick,{}).update(batch)
        reconstructed=cumulative(flow,start)
        assert reconstructed==targets
        checks.append(dict(before_hash=digest(world.external_flow),after_hash=digest(flow),
            before_windows=cumulative(world.external_flow,start),after_windows=reconstructed))
        result.append(replace(world,external_flow=flow))
    record.update(active=True,quantity=points,targets=targets,uniform_shape_products=uniform,worlds=checks,
        batch_hours=[h for h,b in zip(HOURS,batches) if b],
        maximum_rounding_error=max(abs(v-t) for a,b in zip(points,targets) for v,t in zip(a,b)),
        timing_assumption='Six fixed four-step bucket midpoints; historical within-day shape, not exact rival timing.')
    return tuple(result),record


class Integration:
    def __init__(self,directory,mode,trace):
        if mode not in ('shadow','q7'):raise ValueError('Unknown P110 integration mode')
        self.mode=mode
        self.predictor=Predictor(directory)
        self.trace=trace
        self.reset()

    def reset(self):
        self.predictor.last_step=None
        self.predictor.stats={k:(0. if isinstance(v,float) else 0) for k,v in self.predictor.stats.items()}
        self.last_step=None
        self.player=None
        self.observations=0
        self.observation_seconds=0.
        self.overlay_seconds=0.
        self.last_overlay=None
        self.sale_prediction=None
        self.day_sales=DaySales()

    def observe(self,obs):
        start=time.perf_counter();tick=int(obs['step']);player=int(obs['player'])
        if self.last_step is not None and (tick!=self.last_step+1 or player!=self.player):
            raise RuntimeError(('P110 requires one continuous real public prefix',self.last_step,tick,self.player,player))
        if '_public_rival_cargo' not in obs:
            raise RuntimeError('P110 must observe after public cargo feedback')
        self.predictor.observe(obs)
        self.day_sales.observe(obs)
        self.observations+=1;self.last_step=tick;self.player=player
        if tick%24==0:
            self.sale_prediction=None
        if tick%24==0 and 6<=tick//24<28:
            forecast=self.predictor.forecast(obs)
            if self.mode=='q7':
                from top1_planner.p110_sale import make_prediction
                self.sale_prediction=make_prediction(forecast,tick,player)
            head=forecast.get('net_trade_quantity') if forecast is not None else None
            events=forecast.get('net_trade_forecast') if forecast is not None else None
            self.trace.append(dict(kind='P110_PUBLIC_FORECAST',step=tick,observations=self.observations,
                quantity=head,probabilities=events.get('probabilities') if events else None,
                funding_status=events.get('funding_event_status') if events else None,
                adaptive_status=events.get('event_adaptive_status') if events else None,
                learning_state_hash=digest(self.learning_state()),
                scope='Real public prefix. Dawn Q7 quantities enter market quotes and remaining current-day sale decisions. Probabilities, resources and asset forecasts are diagnostic.'))
        if self.sale_prediction is not None:
            obs['_p110_sale_prediction']=self.day_sales.context(self.sale_prediction)
            if tick%4==0 and obs['_p110_sale_prediction']['remaining_debits']:
                self.trace.append(dict(kind='P110_INTRADAY_DEBIT',step=tick,
                    changes=obs['_p110_sale_prediction']['remaining_debits']))
        else:
            obs.pop('_p110_sale_prediction',None)
        self.observation_seconds+=time.perf_counter()-start
        if tick==718:
            self.trace.append(dict(kind='P110_INTEGRATION_COST',step=tick,mode=self.mode,
                observations=self.observations,observe_seconds=self.observation_seconds,
                overlay_seconds=self.overlay_seconds,predictor=dict(self.predictor.stats)))

    def learning_state(self):
        p=self.predictor
        return {name:getattr(p,name).state() if getattr(p,name) is not None else None
                for name in ('reference_mixture','adaptive_sale','adaptive_goods','adaptive_goods_events')}

    def worlds(self,obs,worlds):
        start=time.perf_counter();tick=int(obs['step'])
        if self.last_step!=tick or self.player!=int(obs['player']):
            raise RuntimeError('P110 planning queried before real observation callback')
        value=self.predictor.forecast(obs) if tick%24==0 and 6<=tick//24<28 else None
        if self.mode=='shadow':
            result=worlds
            record=dict(step=tick,mode='shadow',active=False,fallback_reason='logging_only',
                worlds=[dict(before_hash=digest(w.external_flow),after_hash=digest(w.external_flow)) for w in worlds])
        else:
            result,record=overlay(worlds,value,tick)
        self.overlay_seconds+=time.perf_counter()-start
        self.last_overlay=record
        self.trace.append(dict(kind='P110_Q7_MARKET_OVERLAY',**record))
        return result
