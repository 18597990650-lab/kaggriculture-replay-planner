"""Compile production targets into funded actor work from the actual paid state."""
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
import math

from . import rules
from .material import SHEDS, project
from .markets import ScenarioRules
from .targets import ProductionTargets


def distance(a, b):
    return abs(a[0]-b[0]) + abs(a[1]-b[1])


def walk(a, b):
    if a[0] != b[0]:
        return ['EAST' if b[0] > a[0] else 'WEST']
    if a[1] != b[1]:
        return ['SOUTH' if b[1] > a[1] else 'NORTH']
    raise ValueError('Walking requires distinct positions')


def nearest_shed(point):
    return min(SHEDS, key=lambda p: (distance(point, p), p))


@dataclass(frozen=True)
class CompiledProgramme:
    actions: tuple[dict, ...]
    result: dict
    diagnostics: dict


class TaskCompiler:
    """Keep production intent while recomputing work and procurement every tick.

    The observation is immutable. Reservations only bind an actor to a position;
    the next command is derived again from the real tile, bag and shared stock.
    No recorded actions, episode identity or future observations are accepted.
    """

    def __init__(self, targets: ProductionTargets, configuration: dict, *, labor_adjustment=0, tours=None, phase_guidance=False):
        self.targets = targets
        self.cfg = configuration
        for key, value in {'boardSize':10, 'turnsPerDay':24, 'shedCapacity':100,
                           'maxMarketOrdersPerTurn':10}.items():
            if configuration[key] != value:
                raise ValueError('Unsupported task compilation configuration: '+key)
        self.labor_adjustment = labor_adjustment
        if tours is not None and tours.name != targets.name:
            raise ValueError('Service visits and production targets must share their demonstration')
        self.tours = tours
        self.phase_guidance = phase_guidance
        self.tour_cursor = {}
        self.day = None
        self.assignments = {}
        self.realized = set()
        self.diagnostics = Counter()
        self.last_step = None

    def _adopt(self, farm, goals):
        for point, goal in goals.items():
            tile = farm['tiles'][point[1]][point[0]]
            if isinstance(tile, dict) and tile.get('animal',tile.get('crop')) == goal.item:
                self.realized.add(goal.key)

    def _harvestable(self, tile, day):
        if not isinstance(tile, dict) or tile.get('yield_units',0) <= 0:
            return False
        if tile.get('animal'):
            return True
        crop = tile.get('crop')
        if not crop:
            return False
        data = rules.CROPS[crop]
        age = day-tile['planted_day']
        return age >= data['first_yield_day'] and (
            data['ongoing'] or age >= data['max_yield_day']
            or tile['yield_units'] >= data['max_yield'] or day == 29)

    def _fertilizer_gain(self, tile, day):
        if tile.get('fertilized_until_day', -1) >= day or tile.get('watered_today'):
            return 0
        crop = tile['crop']
        data = rules.CROPS[crop]
        age = day-tile['planted_day']
        if not data['ongoing']:
            return min(data['max_yield']-tile['yield_units'],
                sum((data['max_yield_day']+1)//2 <= age+i <= data['max_yield_day']
                    for i in range(min(3,30-day))))
        return sum(age+i+1 >= data['first_yield_day']
            and (age+i+1-data['first_yield_day']) % data['interval'] == 0
            and (age+i+1-data['first_yield_day'])//data['interval'] < data['max_yield']
            for i in range(min(3,29-day)))

    def _work(self, point, farm, private, goals, day, prices, *, harvest_now=False):
        """Return ordered useful tile operations and their scheduling priority."""
        tile = farm['tiles'][point[1]][point[0]]
        goal = goals.get(point)
        if tile == 'LOCKED':
            return [], 0
        ops = []
        value = 0
        if isinstance(tile, dict) and tile.get('animal'):
            item = tile['animal']
            active = goal is not None and goal.item == item
            if active and not tile['fed_today'] and day < 29:
                ops.append(['FEED'])
                value += 1200 if tile['consecutive_unfed'] else 180
            if active and not tile['cared_today'] and day < 29:
                ops.append(['CARE'])
                value += min(250,prices[rules.ANIMALS[item]['product']])
            if tile.get('fertilizer_available'):
                ops.append(['COLLECT_FERTILIZER'])
                value += prices['FERTILIZER']
            if self._harvestable(tile,day):
                ops.append(['HARVEST'])
                value += tile['yield_units']*prices[rules.ANIMALS[item]['product']]
            return ops, value
        if isinstance(tile, dict) and tile.get('crop'):
            crop = tile['crop']
            data = rules.CROPS[crop]
            replacing = goal is not None and goal.key not in self.realized and goal.item != crop
            mature = day-tile['planted_day'] >= data['first_yield_day']
            if self._harvestable(tile,day) or ((replacing or harvest_now) and mature and tile['yield_units']>0):
                if not data['ongoing'] and not tile['watered_today'] and tile['yield_units']<data['max_yield'] and (
                    (data['max_yield_day']+1)//2<=day-tile['planted_day']<=data['max_yield_day']):
                    ops.append(['WATER'])
                    value += prices[crop]
                ops.append(['HARVEST'])
                value += tile['yield_units']*prices[crop]
                if not data['ongoing']:
                    return ops, value
            exhausted = data['ongoing'] and day-tile['planted_day'] >= (
                data['first_yield_day']+(data['max_yield']-1)*data['interval'])
            if replacing and exhausted:
                if not ops:
                    ops.append(['DIG'])
                return ops, value+80
            if day < 29 and not tile['watered_today']:
                gain = self._fertilizer_gain(tile,day)
                if gain*prices[crop] > prices['FERTILIZER'] and gain > 0:
                    ops.append(['FERTILIZE'])
                    value += gain*prices[crop]-prices['FERTILIZER']
                ops.append(['WATER'])
                value += 550 if tile['consecutive_unwatered'] else 95
                if not data['ongoing'] and day-tile['planted_day'] >= (data['max_yield_day']+1)//2:
                    value += prices[crop]
            return ops, value
        if goal is None or goal.key in self.realized:
            return [], 0
        item = goal.item
        first = (rules.CROPS if item in rules.CROPS else rules.ANIMALS)[item]['first_yield_day']
        if day+first >= 30:
            return [], 0
        if item in rules.CROPS:
            if private['seeds'].get(item,0) <= 0:
                return [], 0
            if tile is not None:
                ops.append(['DIG'])
            ops.append(['PLANT',item])
            ops.append(['WATER'])
            return ops, (rules.CROPS[item]['max_yield']*prices[item]+150)/max(1,first/2)
        structure = rules.ANIMALS[item]['structure']
        if not isinstance(tile,dict) or tile.get('kind') != structure:
            if tile is not None:
                ops.append(['DIG'])
            ops.append(['BUILD_'+structure])
        ops.append(['PLACE',item,1])
        return ops, 350+rules.ANIMALS[item]['cost']/2

    def _command(self, actor, point, farm, private, goals, day, prices, *, allowed=None):
        pos = tuple(rules._farmer_position(farm,actor))
        bag = private['inventories'][actor]
        if not self.phase_guidance:
            allowed = None
        ops, value = self._work(point,farm,private,goals,day,prices,
            harvest_now=allowed is not None and 'HARVEST' in allowed)
        if allowed is not None:
            admitted = set(allowed)
            if admitted.intersection(('WATER','FERTILIZE')) and any(op[0]=='PLANT' for op in ops):
                admitted.add('PLANT')
            if 'PLANT' in admitted and any(op[0]=='PLANT' for op in ops):
                admitted.add('DIG')
            if 'PLACE' in admitted and any(op[0]=='PLACE' for op in ops):
                admitted.update(('DIG','BUILD_PASTURE','BUILD_COOP'))
            subset = [op for op in ops if op[0] in admitted]
            value *= len(subset)/max(1,len(ops))
            ops = subset
        skipped = 0
        for op in ops:
            required = 'WHEAT' if op[0]=='FEED' else 'FERTILIZER' if op[0]=='FERTILIZE' else (
                op[1] if op[0]=='PLACE' and op[1] in rules.ANIMALS else None)
            if required and bag.get(required,0) < 1:
                available = private['shed'].get(required,0)
                if available <= 0:
                    skipped += 1
                    continue
                access = nearest_shed(pos)
                if pos != access:
                    return walk(pos,access),value,distance(pos,access)+distance(access,point)+len(ops)+1
                quantity = 1
                if required=='WHEAT':
                    unfed = sum(isinstance(c,dict) and bool(c.get('animal')) and not c['fed_today']
                        for row in farm['tiles'] for c in row)
                    quantity = min(3,max(1,math.ceil(unfed/(len(farm['hands'])+1))))
                return ['PICKUP',required,min(available,quantity)],value,distance(pos,point)+len(ops)+1
            achievable = value*(len(ops)-skipped)/max(1,len(ops))
            if pos != point:
                return walk(pos,point),achievable,distance(pos,point)+len(ops)-skipped
            return op,achievable,len(ops)-skipped
        return None,0,0

    def _delivery(self, actor, farm, private, goals, day, hour, prices, *, requested=None):
        pos = tuple(rules._farmer_position(farm,actor))
        access = nearest_shed(pos)
        travel = distance(pos,access)
        bag = private['inventories'][actor]
        options = []
        for item, count in bag.items():
            if item not in rules.PRODUCTS or count <= 0:
                continue
            if requested is not None and item not in requested:
                continue
            reserve = 0
            if requested is None and hour < 18 and item=='WHEAT' and day<29:
                reserve = min(count,2)
            if requested is None and hour < 17 and item=='FERTILIZER' and day<29:
                useful = any(isinstance(c,dict) and c.get('crop') and self._fertilizer_gain(c,day)>0
                    and self._fertilizer_gain(c,day)*prices[c['crop']]>prices[item]
                    for row in farm['tiles'] for c in row)
                reserve = min(count,1) if useful else 0
            qty = count-reserve
            if qty:
                options.append((qty*prices[item],item,qty))
        if not options or (day<29 and hour+travel+1>=24):
            return None,0
        value,item,qty = max(options)
        if day==29 and hour+travel+1>23:
            return None,0
        urgency = 2 if farm['money']<250 else 1
        if day==29:
            urgency = 2+max(0,hour-12)
        score = urgency*sum(v for v,_,_ in options)/(2*(travel+len(options)))
        return (['PLACE',item,qty] if pos==access else walk(pos,access)), score

    def _preferred_visit(self,actor,farm,private,goals,day,hour,prices):
        if self.tours is None:
            return None
        sequence = self.tours.days[day].get(actor,())
        cursor = self.tour_cursor.get(actor,0)
        while cursor<len(sequence):
            visit = sequence[cursor]
            if visit.kind=='deposit':
                op,_ = self._delivery(actor,farm,private,goals,day,hour,prices,requested=visit.items)
                if op is not None:
                    self.tour_cursor[actor]=cursor
                    return visit
            else:
                op,_,cost = self._command(actor,visit.point,farm,private,goals,day,prices,allowed=visit.operations)
                if op is not None and cost<=24-hour:
                    self.tour_cursor[actor]=cursor
                    return visit
            cursor += 1
        self.tour_cursor[actor]=cursor
        return None

    def _orders(self, farm, private, market, goals, day, hour):
        # Unit actions have already been applied to these disposable copies.
        orders = []
        capacity = self.cfg['shedCapacity']
        def issue(op,item=None,count=1):
            if len(orders)>=10:
                return 0
            if op=='HIRE':
                price = rules._hire_cost(farm['hires_today'],self.cfg.get('farmHandCostMult',1))
                if farm['money']<price:
                    return 0
                rules._do_hire(farm,private,10,self.cfg.get('farmHandCostMult',1))
                orders.append(['HIRE'])
                return 1
            if op=='BUY_LAND':
                before = len(farm['unlocked_quadrants'])
                rules._do_buy_land(farm,10)
                if len(farm['unlocked_quadrants'])>before:
                    orders.append(['BUY_LAND'])
                    return 1
                return 0
            accepted = 0
            for _ in range(count):
                price = rules.CROPS[item]['seed'] if op=='BUY_SEED' else (
                    rules.ANIMALS[item]['cost'] if op=='BUY_ANIMAL' else
                    rules.market_price(item,market['inventory'][item]-(op=='BUY_PRODUCT'),market.get('params')))
                if not rules._commit_unit(op,item,price,farm,private,market,capacity):
                    break
                accepted += 1
            if accepted:
                orders.append([op,item,accepted])
            return accepted

        live = [farm['tiles'][p[1]][p[0]] for p,g in goals.items()
            if g.item in rules.ANIMALS and isinstance(farm['tiles'][p[1]][p[0]],dict)
            and farm['tiles'][p[1]][p[0]].get('animal')==g.item]
        active = len(live)
        unfinished = [g for g in goals.values() if g.key not in self.realized]
        animal_needs = Counter(g.item for g in unfinished if g.item in rules.ANIMALS)
        paid_placements = 0
        for item in animal_needs:
            owned = private['shed'].get(item,0)+sum(b.get(item,0) for b in private['inventories'])
            paid_placements += min(owned,animal_needs[item])
            animal_needs[item] = max(0,animal_needs[item]-owned)
        grain_reserve = active+paid_placements if day<29 else 0
        fert_reserve = min(12,sum(isinstance(c,dict) and bool(c.get('crop'))
            and self._fertilizer_gain(c,day)>0
            and self._fertilizer_gain(c,day)*market['prices'][c['crop']]>market['prices']['FERTILIZER']
            for row in farm['tiles'] for c in row)) if day>=3 and day<29 else 0
        for item in rules.PRODUCTS:
            keep = grain_reserve if item=='WHEAT' else fert_reserve if item=='FERTILIZER' else 0
            issue('SELL',item,max(0,private['shed'].get(item,0)-keep))
        target_workers = max(0,self.targets.daily_workers[day]+self.labor_adjustment)
        if hour<18:
            for _ in range(max(0,min(4,target_workers)-farm['hires_today'])):
                if not issue('HIRE'):
                    break
        if day==0 and hour<8:
            for item,qty in animal_needs.items():
                issue('BUY_ANIMAL',item,qty)
        carried = sum(b.get('WHEAT',0) for b in private['inventories'])
        unfed = sum(not c['fed_today'] for c in live)
        planned = paid_placements+(sum(animal_needs.values()) if day==0 else 0)
        need = max(unfed+planned,active if hour>=20 else 0) if day<29 else 0
        issue('BUY_PRODUCT','WHEAT',max(0,need-carried-private['shed'].get('WHEAT',0)))
        if hour<18:
            for _ in range(max(0,target_workers-farm['hires_today'])):
                spawn = rules._spawn_hand(farm,10)
                available_value = 0
                for y,row in enumerate(farm['tiles']):
                    for x,tile in enumerate(row):
                        work,value = self._work((x,y),farm,private,goals,day,market['prices'])
                        cost = distance(spawn,(x,y))+len(work)
                        if work and cost <= 23-hour:
                            available_value = max(available_value,value)
                price = rules._hire_cost(farm['hires_today'],self.cfg.get('farmHandCostMult',1))
                if available_value<=price:
                    break
                if not issue('HIRE'):
                    break
        if hour<16:
            if any(farm['tiles'][g.point[1]][g.point[0]]=='LOCKED' for g in unfinished):
                issue('BUY_LAND')
            if day>0:
                for item,qty in animal_needs.items():
                    issue('BUY_ANIMAL',item,qty)
            seeds = Counter(g.item for g in unfinished if g.item in rules.CROPS
                and farm['tiles'][g.point[1]][g.point[0]]!='LOCKED'
                and day+rules.CROPS[g.item]['first_yield_day']<30)
            for crop in sorted(seeds,key=lambda c:rules.CROPS[c]['seed']):
                issue('BUY_SEED',crop,max(0,seeds[crop]-private['seeds'].get(crop,0)))
        return orders

    def action(self, observation):
        tick = int(observation['step'])
        day,hour = divmod(tick,24)
        if self.last_step is not None and tick <= self.last_step:
            raise ValueError('Task compiler expects successive real observations')
        self.last_step = tick
        seat = int(observation['player'])
        farm = deepcopy(observation['farms'][seat])
        private = deepcopy(observation['private'])
        market = deepcopy(observation['market'])
        goals = self.targets.at(day)
        if self.day != day:
            self.assignments.clear()
            self.tour_cursor.clear()
            if self.day is None:
                self._adopt(farm,goals)
            self.day = day
        positions = [farm['farmer'],*farm['hands']]
        operations = []
        claimed = set(self.assignments.values())
        for actor in range(len(positions)):
            preferred = self._preferred_visit(actor,farm,private,goals,day,hour,market['prices'])
            chosen = self.assignments.get(actor)
            if self.phase_guidance and chosen is not None and preferred is not None and (
                preferred.kind=='deposit' or preferred.point!=chosen):
                claimed.discard(chosen)
                self.assignments.pop(actor,None)
                chosen = None
            op = None
            score = 0
            if chosen is not None:
                allowed = preferred.operations if preferred is not None and preferred.kind=='work' and preferred.point==chosen else None
                op,value,cost = self._command(actor,chosen,farm,private,goals,day,market['prices'],allowed=allowed)
                if op is not None:
                    score = value/max(1,cost)
                else:
                    claimed.discard(chosen)
                    self.assignments.pop(actor,None)
                    chosen = None
            if op is None:
                best = None
                for y,row in enumerate(farm['tiles']):
                    for x,tile in enumerate(row):
                        point = (x,y)
                        if point in claimed:
                            continue
                        allowed = preferred.operations if preferred is not None and preferred.kind=='work' and preferred.point==point else None
                        command,value,cost = self._command(actor,point,farm,private,goals,day,market['prices'],allowed=allowed)
                        if command is None or cost>24-hour:
                            continue
                        priority = value/max(1,cost)
                        if preferred is not None and preferred.kind=='work' and point==preferred.point:
                            priority *= 4
                        if best is None or priority>best[0]:
                            best = priority,point,command
                if best is not None:
                    score,chosen,op = best
                    claimed.add(chosen)
                    self.assignments[actor] = chosen
                    self.diagnostics['assigned_tile_tasks'] += 1
                    if preferred is not None and preferred.kind=='work' and chosen==preferred.point:
                        self.diagnostics['demonstrated_visits_used'] += 1
            delivery,delivery_score = self._delivery(actor,farm,private,goals,day,hour,market['prices'])
            if preferred is not None and preferred.kind=='deposit':
                delivery,delivery_score = self._delivery(actor,farm,private,goals,day,hour,market['prices'],requested=preferred.items)
                delivery_score *= 4
            if delivery is not None and delivery_score>score:
                op = delivery
                if chosen is not None:
                    claimed.discard(chosen)
                    self.assignments.pop(actor,None)
                self.diagnostics['delivery_steps'] += 1
            op = op or ['PASS']
            pos = tuple(rules._farmer_position(farm,actor))
            before_effect = None
            if op[0]!='PASS' and op[0] not in rules.FARMER_MOVES:
                before_effect = deepcopy((farm['tiles'][pos[1]][pos[0]],private['inventories'][actor],
                    private['seeds'],private['shed']))
            rules._apply_unit_action(farm,private,actor,op,10,day,24,self.cfg['shedCapacity'])
            tile = farm['tiles'][pos[1]][pos[0]]
            if before_effect is not None and before_effect==(tile,private['inventories'][actor],private['seeds'],private['shed']):
                self.diagnostics['no_effect_'+op[0]] += 1
            if op[0] in ('PLACE','PLANT') and pos in goals and isinstance(tile,dict):
                if tile.get('animal',tile.get('crop'))==goals[pos].item:
                    self.realized.add(goals[pos].key)
            operations.append(op)
            self.diagnostics['action_'+op[0]] += 1
        return dict(farmer=operations[0],hands=operations[1:],
            market=self._orders(farm,private,market,goals,day,hour))


def compile_programme(observation, configuration, targets, world, *, labor_adjustment=0, continuation=None, tours=None, phase_guidance=False):
    """Generate and value a complete successor under one explicit market world."""
    if continuation is not None:
        if continuation.targets != targets or continuation.cfg != configuration:
            raise ValueError('Continuation must belong to the same target and configuration')
        controller = deepcopy(continuation)
    else:
        controller = TaskCompiler(targets,configuration,labor_adjustment=labor_adjustment,tours=tours,phase_guidance=phase_guidance)
    route = [dict(farmer=['PASS'],hands=[],market=[]) for _ in range(720)]
    model = ScenarioRules(int(observation['player']),int(observation['step']),
        world.future_shops,world.external_flow,world.scale,consumption=world.additional_consumption)
    def choose(own, unused):
        model.prepare(own,configuration)
        action = controller.action(own)
        route[own['step']] = action
        return action
    result = project(observation,route,configuration,model,fund=False,record=True,transform=choose)
    return CompiledProgramme(tuple(route[:719]),result,dict(controller.diagnostics))
