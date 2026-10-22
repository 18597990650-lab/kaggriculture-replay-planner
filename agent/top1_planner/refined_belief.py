"""Account for visible harvests that have not reappeared as public market supply."""
from collections import Counter

from . import rules
from .public_harvest import midnight_harvest

GOODS=tuple(p for p in rules.PRODUCTS if p not in ('WHEAT','FERTILIZER'))


def _product(tile):
    if not isinstance(tile,dict):
        return None
    return rules.ANIMALS[tile['animal']]['product'] if tile.get('animal') else tile.get('crop')


class PublicSupplyBelief:
    """A stock-flow estimate from observations and our own requested sales.

    Harvest-versus-discard ambiguity and one-dollar transactions remain
    unresolved. These quantities are estimates, never private-stock facts.
    """
    def __init__(self):
        self.previous=None
        self.sold_upper=Counter()
        self.pending=Counter()
        self.started_at_zero=False
        self.uncertain=set()

    def observe(self,observation):
        tick=int(observation['step'])
        self.harvest_checks=[]
        if self.previous is None:
            self.started_at_zero=tick==0
            if tick:
                self.uncertain.update(GOODS)
        else:
            old=self.previous
            if tick!=old['step']+1:
                raise ValueError('Public supply accounting requires consecutive observations')
            old_farm=old['farm']
            farm=observation['farms'][1-int(observation['player'])]
            position_counts=Counter(tuple(p) for p in [old_farm['farmer'],*old_farm['hands']])
            positions=set(position_counts)
            potential=[[dict(tile) if isinstance(tile,dict) else tile for tile in row] for row in old_farm['tiles']]
            shadow=dict(old_farm,tiles=potential)
            rules._decay_plants(shadow,old['step'])
            if old['step']%24==23:
                rules._daily_refresh_plants(shadow,old['step']//24,24)
                rules._daily_refresh_animals(shadow,old['step']//24)
            for y,row in enumerate(old_farm['tiles']):
                for x,tile in enumerate(row):
                    item=_product(tile)
                    if item not in GOODS or (x,y) not in positions:
                        continue
                    if tile.get('crop') and old['step']//24-tile['planted_day']<rules.CROPS[item]['first_yield_day']:
                        continue
                    later=farm['tiles'][y][x]
                    expected=potential[y][x]
                    before_qty=expected.get('yield_units',0) if _product(expected)==item else tile.get('yield_units',0)
                    after_qty=later.get('yield_units',0) if _product(later)==item else 0
                    amount=max(0,min(tile.get('yield_units',0),before_qty-after_qty))
                    resolved=midnight_harvest(tile,later,old['step'],position_counts[(x,y)],rules)
                    if resolved is not None:
                        self.harvest_checks.append(dict(position=[x,y],product=item,before=amount,after=resolved))
                        amount=resolved
                    self.pending[item]+=amount
            consumption=Counter()
            if old['step']%4==0:
                for shop in old['shops']:
                    products=rules.SHOPS[shop]
                    for item in products:
                        consumption[item]+=2 if len(products)==1 else 1
            if old['step']%24==0:
                consumption.update({p:1 for p in rules.TOWN_CENTER_PRODUCTS})
            for item in GOODS:
                addition=observation['market']['inventory'][item]-old['inventory'][item]+consumption[item]
                own_upper=self.sold_upper[item] if rules.market_price(item,old['inventory'][item],old['params'])>1 else 0
                rival_min=max(0,addition-own_upper)
                self.pending[item]=max(0,self.pending[item]-rival_min)
                if rules.market_price(item,old['inventory'][item],old['params'])==1 or observation['market']['prices'][item]==1:
                    self.uncertain.add(item)
                elif self.pending[item]==0:
                    self.uncertain.discard(item)
        farm=observation['farms'][1-int(observation['player'])]
        self.previous=dict(step=tick,inventory=dict(observation['market']['inventory']),params=observation['market'].get('params'),
                           shops=tuple(observation['town']['unlocked_shops']),
                           farm=dict(farm,farmer=list(farm['farmer']),hands=[list(p) for p in farm['hands']],
                                     tiles=[[dict(t) if isinstance(t,dict) else t for t in row] for row in farm['tiles']]))
        return dict(origin=tick,quantities={p:q for p,q in self.pending.items() if q and p not in self.uncertain},
                    uncertain_upper={p:q for p,q in self.pending.items() if q and p in self.uncertain},
                    started_at_zero=self.started_at_zero,
                    evidence='Estimate from public tile/market transitions and our own sales; discard and floor-price ambiguity remain')

    def remember_action(self,action):
        self.sold_upper=Counter()
        for order in action['market'][:10]:
            if len(order)>=3 and order[0]=='SELL' and order[1] in GOODS:
                self.sold_upper[order[1]]+=max(0,int(order[2]))
