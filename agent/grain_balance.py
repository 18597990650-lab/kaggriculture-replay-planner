"""Estimate total wheat from public harvest/service changes and qualified trades."""
from collections import Counter
from top1_planner import rules


class WheatBalance:
    def __init__(self):
        self.previous=None
        self.estimate=0.
        self.started_at_zero=False
        self.unknown_trades=0
        self.harvest_candidates=0
        self.ambiguous_harvests=0
        self.visible_feeds=0

    def observe(self,observation,resource_flow):
        tick=int(observation['step'])
        if resource_flow['origin']!=tick:raise ValueError('Resource flow does not match the observation')
        farm=observation['farms'][1-int(observation['player'])]
        harvest=0;feeds=0;uncertain=0
        if self.previous is None:
            self.started_at_zero=tick==0
        else:
            old=self.previous
            if tick!=old['step']+1:raise ValueError('Wheat accounting requires consecutive observations')
            positions=Counter(tuple(position) for position in [old['farm']['farmer'],*old['farm']['hands']])
            day=old['step']//24
            for y,row in enumerate(old['farm']['tiles']):
                for x,before in enumerate(row):
                    after=farm['tiles'][y][x]
                    actors=positions[(x,y)]
                    if isinstance(before,dict) and before.get('crop')=='WHEAT' and actors:
                        same=isinstance(after,dict) and after.get('crop')=='WHEAT' and after.get('planted_day')==before.get('planted_day')
                        mature=day-before['planted_day']>=rules.CROPS['WHEAT']['first_yield_day']
                        if mature and not same:
                            harvest+=before.get('yield_units',0)
                            self.harvest_candidates+=1
                            uncertain+=int(actors>1 or before.get('max_lifespan_step',10**9)<=old['step'])
                    if not isinstance(after,dict) or not after.get('animal') or not actors:continue
                    same=isinstance(before,dict) and before.get('animal')==after['animal'] and before.get('placed_day')==after['placed_day']
                    was_fed=bool(before.get('fed_today')) if same else False
                    fed=bool(after.get('fed_today')) if tick%24 else after.get('consecutive_unfed')==0
                    feeds+=int(fed and not was_fed)
            trade=resource_flow['last_transaction'][0]
            if trade is None:self.unknown_trades+=1
            self.estimate=max(0.,self.estimate+harvest-feeds-(trade if trade is not None else 0))
            self.visible_feeds+=feeds
            self.ambiguous_harvests+=uncertain
        self.previous=dict(step=tick,farm=dict(farm,farmer=list(farm['farmer']),hands=[list(pos) for pos in farm['hands']],
                           tiles=[[dict(tile) if isinstance(tile,dict) else tile for tile in row] for row in farm['tiles']]))
        return dict(origin=tick,point=self.estimate,harvest_change=harvest,feed_change=feeds,
                    unknown_trades=self.unknown_trades,harvest_candidates=self.harvest_candidates,
                    ambiguous_harvests=self.ambiguous_harvests,visible_feeds=self.visible_feeds,started_at_zero=self.started_at_zero,
                    boundary='Point estimate only: unseen resource trades, deliberate clearing, simultaneous watering and inventory discard remain uncertain.')
