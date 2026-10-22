"""Debit certified realized sales from a fixed dawn remaining-supply scenario."""
from copy import deepcopy

GOODS=('CARROT','TOMATO','STRAWBERRY','MELON','EGG','MILK','WOOL')


class DaySales:
    def __init__(self):
        self.origin=None
        self.last_step=None
        self.known=[0]*7
        self.unknown=[0]*7

    def observe(self,observation):
        tick=int(observation['step'])
        if self.last_step is not None and tick!=self.last_step+1:
            raise ValueError('Remaining-sale feedback requires a continuous real prefix')
        flow=observation['_public_rival_cargo']['goods_flow']
        if flow['origin']!=tick or tuple(flow['products'])!=GOODS:
            raise ValueError('Certified goods feedback does not match the current observation')
        if tick%24==0:
            self.origin=tick;self.known=[0]*7;self.unknown=[0]*7
        else:
            for i,value in enumerate(flow['last_transaction']):
                if value is None:self.unknown[i]+=1
                elif int(value)!=value or value<0:raise ValueError('Invalid certified goods sale')
                else:self.known[i]+=int(value)
        self.last_step=tick

    def context(self,prediction,mode='front'):
        if prediction is None:return None
        if prediction['origin']!=self.origin:
            raise ValueError('Remaining forecast and sale feedback have different origins')
        if mode not in ('front','proportional'):
            raise ValueError('Unknown remaining-sale redistribution')
        value=deepcopy(prediction);tick=self.last_step;changes={}
        for i,product in enumerate(GOODS):
            entries=[(t,-batch.get(product,0)) for t,batch in sorted(value['flow'].items()) if t>=tick]
            original=sum(q for _,q in entries)
            cap=max(0,prediction['targets'][2][i]-self.known[i])
            total=min(original,cap)
            if total==original:continue
            if mode=='proportional':
                from p110_integration import apportion
                amounts=apportion(total,[q for _,q in entries])
            else:
                excess=original-total;amounts=[]
                for _,q in entries:
                    removed=min(q,excess);amounts.append(q-removed);excess-=removed
                assert excess==0
            for (t,_),q in zip(entries,amounts):
                if q:value['flow'][t][product]=-q
                else:value['flow'][t].pop(product,None)
            changes[product]=dict(before=original,after=total,known_completed=self.known[i],unknown_steps=self.unknown[i])
        value['asof']=tick;value['remaining_debits']=changes
        return value
