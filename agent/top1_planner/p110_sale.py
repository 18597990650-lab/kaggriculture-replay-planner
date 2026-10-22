"""Use observed dawn quantities as a read-only, current-day sale scenario."""
from dataclasses import dataclass

GOODS=('CARROT','TOMATO','STRAWBERRY','MELON','EGG','MILK','WOOL')
CONTEXT='_p110_sale_prediction'


@dataclass(frozen=True)
class _FlowWorld:
    external_flow: dict
    scale: float=1.


def make_prediction(forecast,origin,player):
    from p110_integration import overlay
    worlds,record=overlay((_FlowWorld({}),),forecast,origin)
    if not record['active']:
        return None
    return dict(origin=origin,end=origin+24,player=player,
        flow=worlds[0].external_flow,targets=record['targets'])


def active_prediction(observation):
    prediction=observation.get(CONTEXT)
    if prediction is None:
        return None
    tick=int(observation['step'])
    if int(observation['player'])!=prediction['player']:
        raise ValueError('A daily sale forecast belongs to its observer seat')
    if prediction['end']!=prediction['origin']+24 or prediction['origin']%24:
        raise ValueError('A daily sale forecast must cover exactly its originating day')
    if tick<prediction['origin']:
        raise ValueError('Sale forecast cannot be used before its observed origin')
    return prediction if tick<prediction['end'] else None


def overlay_scenarios(observation,scenarios,windows,prediction,end):
    """Replace rival goods arrivals today, retaining every other event stream."""
    start=int(observation['step'])
    stop=min(end,prediction['end']-1)
    revised=[]
    new_windows=set(windows)
    for scenario in scenarios:
        events={}
        for tick,own,rival,demand in scenario:
            remaining=dict(rival)
            if start<=tick<=stop:
                for item in GOODS:
                    remaining.pop(item,None)
            events[tick]=(dict(own),remaining,dict(demand))
        for tick,batch in prediction['flow'].items():
            if not start<=tick<=stop:
                continue
            own,rival,demand=events.setdefault(tick,({},{},{}))
            for item,value in batch.items():
                if item not in GOODS or value>0 or int(value)!=value:
                    raise ValueError('Sale context requires integer sale-only quantities')
                rival[item]=-value
        imminent=[tick for tick,(_,rival,_) in events.items()
                  if start+1<tick<=min(start+24,stop) and any(rival.values())]
        if imminent:
            new_windows.add(min(imminent)-1)
        revised.append(events)
    output=[]
    for events in revised:
        for tick in new_windows:
            events.setdefault(tick,({},{},{}))
        output.append([(tick,*events[tick]) for tick in sorted(events)])
    return output,sorted(new_windows)
