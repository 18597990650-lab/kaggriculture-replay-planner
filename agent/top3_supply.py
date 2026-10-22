"""Blend the visible-asset forecast with nearest public demonstrated economies."""
from collections import Counter
from dataclasses import replace
from top1_planner import rules

def observed_features(obs,model):
    farm=obs['farms'][1-int(obs['player'])]
    assets=Counter((rules.ANIMALS[t['animal']]['product'] if t.get('animal') else t.get('crop'))
        for row in farm['tiles'] for t in row if isinstance(t,dict))
    shops=Counter(obs['town']['unlocked_shops'])
    return [assets[p] for p in model['products']]+[shops[p] for p in model['shops']]

def blend(observation,worlds,model,weight):
    day=int(observation['step'])//24;start=int(observation['step'])
    if day<6 or day>=28:return worlds
    f=observed_features(observation,model)
    def distance(sample):
        other=sample['features'][day]
        return sum(((a-b)/(4 if i<9 else 1))**2 for i,(a,b) in enumerate(zip(f,other)))
    neighbors=sorted(model['samples'],key=distance)[:4]
    weights=[1/(1+distance(s)) for s in neighbors];normalizer=sum(weights)
    output=[]
    for world in worlds:
        flow={t:dict(batch) for t,batch in world.external_flow.items()}
        # Today's observed cargo and delivery forecast stay intact. The prior
        # addresses later unobserved renewal and expansion, not past sales.
        for tick,batch in flow.items():
            if tick//24>day:
                for item,q in list(batch.items()):batch[item]=int(round(q*(1-weight)))
        for future in range(day+1,30):
            for i,item in enumerate(model['products']):
                sales=sum(w*s['net_sales'][future][i] for w,s in zip(weights,neighbors))/normalizer
                net=int(round(-weight*sales))
                first=int(net/2)
                for hour,q in ((8,first),(18,net-first)):
                    tick=future*24+hour
                    if q and start<=tick<719:
                        batch=flow.setdefault(tick,{})
                        batch[item]=batch.get(item,0)+q
        output.append(replace(world,external_flow=flow,label=world.label+f'/public_top3_supply_prior_{weight:g}'))
    return tuple(output)
