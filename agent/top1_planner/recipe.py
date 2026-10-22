"""Execute ordered production tasks with current-state material and funding checks."""
import gc
import threading
from collections import Counter
from copy import deepcopy
from functools import lru_cache

from . import rules
from .compiler import CompiledProgramme, distance, nearest_shed, walk
from .markets import ScenarioRules
from .material import SHEDS, project, clone_farm, clone_private, clone_market, clone_action_farm
from .settlement import sale_caps
from .work_dispatch import take_paid_visit
from .deadline_order import defer_optional_water
from .terminal_delivery import last_chance_delivery, final_orders
from .calendar import WorkCalendar, productive_fertilizer_window
from .redeployment import managed, identity
from .crop_input_value import fertilizer_due
from .establishment_order import fit_first_watering
from .service_cycle import service_required
from .sale_order import rank_sales
from .programmes import Task
from .orphan_harvest import expiring_crops, take_orphan_harvest
from .critical_water import take_critical_water
from .survival_water import take_survival_water
from .establishment_precedence import ensure_establishment_precedence
from .due_feed_order import prioritize_due_feed
from .successor_service import protect_successor_service, service_actors


@lru_cache(maxsize=2048)
def _labor_obligation(hired, today, tomorrow, multiplier):
    return (sum(rules._hire_cost(i, multiplier) for i in range(hired, today))
            + sum(rules._hire_cost(i, multiplier) for i in range(tomorrow)))


class RecipeCompiler:
    """Own a paid continuation; derive moves and quantities from actual resources."""

    def __init__(self, programme, configuration, *, settlement=False, bind_herd=False, adaptive_crops=False):
        for key, value in {'boardSize': 10, 'turnsPerDay': 24, 'shedCapacity': 100,
                           'maxMarketOrdersPerTurn': 10}.items():
            if configuration[key] != value:
                raise ValueError('Unsupported recipe configuration: ' + key)
        self.programme = programme
        self.cfg = configuration
        self.day = None
        self.cursor = {}
        self.last_step = None
        self.diagnostics = Counter()
        self.settlement = settlement
        self.settlement_records = []
        self.terminal_records = []
        self.bind_herd=bind_herd
        self.adaptive_crops=adaptive_crops
        self.calendar=WorkCalendar(programme) if adaptive_crops else None
        self.allocation_records=[]
        self.repair_checked=None
        self.lot_harvest=Counter()
        self.lot_bindings={}
        self.day_sequences=None
        self.dispatch_records=[]
        self.deadline_records=[]
        self.establishment_records=[]
        self.survival_approved={}
        self.survival_denied={}
        self.survival_admission=None
        self.successor_service_rows = None
        self.successor_service_actors = frozenset()
        self._warehouse_sequences = {}
        self._procurement_sequences = {}
        self._establishment_sequences = {}

    def can_continue(self,programme):
        """A changed future may inherit execution only after an identical prefix."""
        if programme==self.programme:
            return True
        if self.day is None or self.last_step is None:
            return False
        stop=self.day
        if (programme.days[:stop]!=self.programme.days[:stop]
                or programme.workers[:stop]!=self.programme.workers[:stop]):
            return False
        if (programme.days[stop]==self.programme.days[stop]
                and programme.workers[stop]==self.programme.workers[stop]):
            return True
        previous=self._tasks(stop)
        for actor in set(previous)|set(programme.days[stop]):
            count=self.cursor.get(actor,0)
            if previous.get(actor,())[:count]!=programme.days[stop].get(actor,())[:count]:
                return False
        return True

    def fork(self, *, settlement=None, programme=None):
        """Share read-only programme data and copy only the execution state."""
        target=self.programme if programme is None else programme
        if not self.can_continue(target):
            raise ValueError('A changed paid prefix cannot inherit this execution state')
        child = RecipeCompiler(target, dict(self.cfg),
                               settlement=self.settlement if settlement is None else settlement,
                               bind_herd=self.bind_herd,adaptive_crops=self.adaptive_crops)
        child.day, child.last_step = self.day, self.last_step
        child.cursor = dict(self.cursor)
        child.diagnostics = self.diagnostics.copy()
        child.calendar=self.calendar.fork(programme=target) if self.calendar is not None else None
        if child.calendar is not None and self.day is not None and target.days[self.day]!=self.programme.days[self.day]:
            child.calendar.sequences=dict(target.days[self.day])
            replaced={t.point for seq in target.days[self.day].values() for t in seq if t.operation in ('PLANT','PLACE') and t.lot}
            child.calendar.pending={p:job for p,job in child.calendar.pending.items() if p not in replaced}
        child.repair_checked=self.repair_checked
        child.lot_harvest=self.lot_harvest.copy()
        child.lot_bindings=dict(self.lot_bindings)
        child.survival_approved=dict(self.survival_approved)
        child.survival_denied=dict(self.survival_denied)
        if self.day_sequences is not None:
            child.day_sequences=dict(self.day_sequences if target.days[self.day]==self.programme.days[self.day]
                                     else target.days[self.day])
        return child

    def _tile(self, farm, task):
        return farm['tiles'][task.point[1]][task.point[0]]

    def _successor_matches(self, task, tile):
        found=identity(task,tile)
        actual=(task.point,*found) if found else None
        bound=self.lot_bindings.get(task.lot)
        if bound is not None:
            return actual==bound
        if actual is not None and actual not in self.lot_bindings.values():
            self.lot_bindings[task.lot]=actual
            return True
        return False

    def _established(self, task, tile):
        if managed(task):
            self._successor_matches(task,tile)
            return task.lot in self.lot_bindings
        if task.operation=='PLACE':
            return isinstance(tile,dict) and (bool(tile.get('animal')) if self.bind_herd else tile.get('animal')==task.item)
        return isinstance(tile,dict) and tile.get('crop')==task.item and tile['planted_day']>=task.generation

    def _tasks(self,day):
        if day==self.day and self.day_sequences is not None:
            return self.day_sequences
        if self.calendar is not None and self.calendar.day==day:
            return self.calendar.sequences
        return self.programme.days[day]

    def _remaining(self, actor, day):
        return self._tasks(day).get(actor, ())[self.cursor.get(actor, 0):]

    def _establishment_pending(self, task, day):
        expected = 'PLACE' if task.item in rules.ANIMALS else 'PLANT'
        key=(expected,task.point,task.item,task.generation)
        for actor,sequence in self._tasks(day).items():
            cached=self._establishment_sequences.get(actor)
            if cached is None or cached[0] is not sequence:
                entries={}
                for index,other in enumerate(sequence):
                    if other.operation in ('PLANT','PLACE'):
                        signature=(other.operation,other.point,other.item,other.generation)
                        entries.setdefault(signature,[]).append(index)
                cached=self._establishment_sequences[actor]=(sequence,entries)
            indices=cached[1].get(key,())
            cursor=self.cursor.get(actor,0)
            if any(index>=cursor for index in indices):
                return expected!='PLANT' or day+rules.CROPS[task.item]['first_yield_day']<=29
        return False

    def _trip_inputs(self, actor, farm, day, start, wanted=None):
        """Required starting stock before the next supply stop, net of own collection."""
        balance, required = Counter(), Counter()
        seen = set()
        closed = set()
        for task in self._tasks(day).get(actor, ())[start:]:
            op, item = task.operation, task.item
            tile = self._tile(farm, task)
            if not service_required(task, tile, day):
                continue
            if managed(task) and op in ('FEED','CARE','HARVEST','COLLECT_FERTILIZER','WATER','FERTILIZE') and not self._successor_matches(task,tile) and not self._establishment_pending(task,day):
                continue
            if managed(task) and op=='FERTILIZE' and not fertilizer_due(tile,day):
                continue
            if op == 'PICKUP':
                closed.add(item)
                continue
            material = None
            if op == 'FEED' and not (isinstance(tile, dict) and tile.get('fed_today')):
                material = 'WHEAT'
            elif op == 'FERTILIZE' and (productive_fertilizer_window(tile,day) if self.adaptive_crops else
                    not (isinstance(tile, dict) and tile.get('fertilized_until_day', -1) >= day + 2)):
                material = 'FERTILIZER'
            elif op == 'PLACE' and item in rules.ANIMALS and not self._established(task,tile):
                material = item
            if material is not None and material not in closed and (task.point, op) not in seen:
                seen.add((task.point, op))
                balance[material] -= 1
                required[material] = max(required[material], -balance[material])
            if op == 'COLLECT_FERTILIZER' and isinstance(tile, dict) and tile.get('fertilizer_available'):
                balance['FERTILIZER'] += 1
            if op == 'HARVEST' and isinstance(tile, dict) and tile.get('crop') == 'WHEAT' and (
                    day - tile['planted_day'] >= rules.CROPS['WHEAT']['first_yield_day']):
                balance['WHEAT'] += tile.get('yield_units', 0)
        return required if wanted is None else required[wanted]

    def _warehouse_inputs(self, actor, farm, private, day):
        """Credit only this actor's carried stock and collection before consumption."""
        bag = private['inventories'][actor] if actor < len(private['inventories']) else {}
        balance = {'WHEAT': bag.get('WHEAT', 0), 'FERTILIZER': bag.get('FERTILIZER', 0)}
        required, seen = {}, set()
        sequence = self._tasks(day).get(actor, ())
        cached = self._warehouse_sequences.get(actor)
        if cached is None or cached[0] is not sequence:
            entries = tuple((index, task) for index, task in enumerate(sequence)
                if task.operation in ('FEED', 'FERTILIZE', 'HARVEST', 'COLLECT_FERTILIZER')
                or task.operation in ('CARE', 'WATER') and managed(task))
            cached = self._warehouse_sequences[actor] = (sequence, entries)
        cursor = self.cursor.get(actor, 0)
        for index, task in cached[1]:
            if index < cursor:
                continue
            tile = self._tile(farm, task)
            op = task.operation
            if not service_required(task, tile, day):
                continue
            if managed(task) and op in ('FEED','CARE','HARVEST','COLLECT_FERTILIZER','WATER','FERTILIZE') and not self._successor_matches(task,tile) and not self._establishment_pending(task,day):
                continue
            if managed(task) and op=='FERTILIZE' and not fertilizer_due(tile,day):
                continue
            key = (task.point, op, task.generation)
            if key in seen:
                continue
            seen.add(key)
            if op == 'COLLECT_FERTILIZER' and isinstance(tile, dict) and tile.get('fertilizer_available'):
                balance['FERTILIZER'] += 1
            if op == 'HARVEST' and isinstance(tile, dict) and tile.get('crop') == 'WHEAT' and (
                    day - tile['planted_day'] >= rules.CROPS['WHEAT']['first_yield_day']):
                balance['WHEAT'] += tile['yield_units']
            material = None
            if op == 'FEED' and not (isinstance(tile, dict) and tile.get('fed_today')):
                if (isinstance(tile, dict) and (bool(tile.get('animal')) if self.bind_herd else tile.get('animal') == task.item)) or self._establishment_pending(task, day):
                    material = 'WHEAT'
            if op == 'FERTILIZE' and (productive_fertilizer_window(tile,day) if self.adaptive_crops else
                    isinstance(tile, dict) and tile.get('crop') == task.item and tile.get('fertilized_until_day', -1) < day + 2):
                material = 'FERTILIZER'
            if material is not None:
                if balance[material] > 0:
                    balance[material] -= 1
                else:
                    required[material] = required.get(material, 0)+1
        return required

    def _supply(self, actor, item, quantity, farm, private):
        pos = tuple(rules._farmer_position(farm, actor))
        access = nearest_shed(pos)
        have = private['inventories'][actor].get(item, 0)
        needed = max(0, quantity - have)
        if not needed:
            return None
        if pos != access:
            return walk(pos, access)
        take = min(needed, private['shed'].get(item, 0))
        return ['PICKUP', item, take] if take else ['PASS']

    def _deposit(self, actor, task, farm, private, day):
        bag = private['inventories'][actor]
        reserve = self._trip_inputs(actor, farm, day, self.cursor.get(actor, 0) + 1)
        available = {item: max(0, qty - reserve[item]) for item, qty in bag.items()
                     if task.operation == 'DROP' or task.item == item}
        available = {item: qty for item, qty in available.items() if qty}
        if not available:
            return None
        pos = tuple(rules._farmer_position(farm, actor))
        item = max(available, key=lambda key: available[key])
        accesses = SHEDS
        if item in rules.ANIMALS:
            structure = rules.ANIMALS[item]['structure']
            safe = [point for point in SHEDS if not (isinstance(farm['tiles'][point[1]][point[0]], dict)
                    and farm['tiles'][point[1]][point[0]].get('kind') == structure
                    and 'animal' not in farm['tiles'][point[1]][point[0]])]
            if safe:
                accesses = safe
        access = min(accesses, key=lambda point: (distance(pos, point), point))
        if pos != access:
            return walk(pos, access)
        room = self.cfg['shedCapacity'] - sum(private['shed'].values())
        if room <= 0:
            return ['PASS']
        if available == dict(bag) and sum(available.values()) <= room:
            return ['DROP']
        if item in rules.ANIMALS:
            standing = farm['tiles'][pos[1]][pos[0]]
            if isinstance(standing, dict) and standing.get('kind') == rules.ANIMALS[item]['structure'] and 'animal' not in standing:
                return ['PASS']
        return ['PLACE', item, min(room, available[item])]

    def _command(self, actor, task, farm, private, day):
        """None means completed or obsolete; PASS retains an unmet dependency."""
        op, item = task.operation, task.item
        tile = self._tile(farm, task)
        if op=='PLANT' and day+rules.CROPS[item]['first_yield_day']>29:
            return None
        if not service_required(task, tile, day):
            self.diagnostics['cycle_pause_' + op] += 1
            return None
        if self.calendar is not None:
            self.calendar.visit(task)
        if managed(task):
            if op=='FERTILIZE' and not fertilizer_due(tile,day):
                return None
            if op in ('PLANT','PLACE') and self._established(task,tile):
                return None
            if op in ('WATER','FERTILIZE','HARVEST','FEED','CARE','COLLECT_FERTILIZER') and not self._successor_matches(task,tile):
                return None
        if self.bind_herd and not managed(task) and op in ('FEED','CARE') and isinstance(tile,dict) and tile.get('animal'):
            item=tile['animal']
        if task.lot and not managed(task) and op in ('WATER','FERTILIZE','HARVEST') and identity(task,tile) is None:
            return None
        if self.adaptive_crops and op in ('WATER','FERTILIZE') and isinstance(tile,dict) and tile.get('crop'):
            item=tile['crop']
        if op in ('WATER','FERTILIZE') and isinstance(tile,dict) and tile.get('crop')==item:
            crop=rules.CROPS[item]
            age=day-tile['planted_day']
            finite_ready=not crop['ongoing'] and tile['yield_units']>0 and age>=crop['first_yield_day']
            yield_complete=tile['yield_units']>=crop['max_yield'] or age>crop['max_yield_day']
            final_visit=(self.last_step+1)%24==0 and self.last_step+1>=tile['max_lifespan_step']>=0
            if finite_ready and (yield_complete or final_visit):
                op='HARVEST'
            elif self.adaptive_crops and op=='FERTILIZE' and not productive_fertilizer_window(tile,day):
                return None
        pos = tuple(rules._farmer_position(farm, actor))
        bag = private['inventories'][actor]
        if op == 'CLEAR_CROP':
            if not (isinstance(tile,dict) and tile.get('crop')==item
                    and tile.get('planted_day')==task.generation):
                return None
            if pos!=task.point:
                return walk(pos,task.point)
            mature=day-tile['planted_day']>=rules.CROPS[item]['first_yield_day']
            return ['HARVEST'] if mature and tile.get('yield_units',0)>0 else ['DIG']
        if op == 'PICKUP':
            amount = self._trip_inputs(actor, farm, day, self.cursor.get(actor, 0) + 1, item)
            return self._supply(actor, item, amount, farm, private)
        if op in ('DROP', 'DEPOSIT'):
            return self._deposit(actor, task, farm, private, day)
        if op in ('WATER', 'FERTILIZE', 'FEED', 'CARE'):
            matches = isinstance(tile, dict) and tile.get('animal', tile.get('crop')) == item
            if not matches and not self._establishment_pending(task, day):
                self.diagnostics['obsolete_' + op] += 1
                return None
        if isinstance(tile, dict):
            if op == 'FEED' and tile.get('fed_today'):
                return None
            if op == 'CARE' and tile.get('cared_today'):
                return None
            if op == 'WATER' and tile.get('crop') == item and tile['watered_today']:
                return None
            if op == 'FERTILIZE' and tile.get('crop') == item and tile.get('fertilized_until_day', -1) >= day + 2:
                return None
            if op == 'PLACE' and not managed(task) and (bool(tile.get('animal')) if self.bind_herd else tile.get('animal') == item):
                return None
            if op == 'PLANT' and not managed(task) and tile.get('crop') == item and tile['planted_day'] >= task.generation:
                return None
            if op.startswith('BUILD_') and tile.get('kind') == op[6:]:
                return None
        if op == 'HARVEST':
            if not isinstance(tile, dict) or not tile.get('yield_units'):
                return None
            if tile.get('crop') and day - tile['planted_day'] < rules.CROPS[tile['crop']]['first_yield_day']:
                self.diagnostics['immature_harvest_deferred'] += 1
                return None
        if op == 'COLLECT_FERTILIZER' and not (isinstance(tile, dict) and tile.get('fertilizer_available')):
            return None
        if op == 'DIG':
            if tile is None or (isinstance(tile, dict) and tile.get('animal')):
                return None
            if isinstance(tile, dict) and tile.get('crop'):
                crop = rules.CROPS[tile['crop']]
                age = day - tile['planted_day']
                if tile['yield_units'] and age >= crop['first_yield_day']:
                    return walk(pos, task.point) if pos != task.point else ['HARVEST']
                expiry = crop['first_yield_day'] + (crop['max_yield'] - 1) * crop['interval']
                if not crop['ongoing'] or age < expiry:
                    if self.calendar is not None:
                        self.diagnostics['deferred_paid_crop_clearance']+=1
                        return None
                    return ['PASS']
        if self.calendar is not None and (op=='PLANT' or op.startswith('BUILD_') or op=='PLACE') and (
                isinstance(tile,dict) and tile.get('crop')):
            crop=rules.CROPS[tile['crop']]
            age=day-tile['planted_day']
            expiry=crop['first_yield_day']+(crop['max_yield']-1)*crop['interval']
            ready=tile.get('yield_units',0) and age>=crop['first_yield_day']
            cleared=bool(crop['ongoing'] and age>=expiry and not tile.get('yield_units',0))
            if not ready and not cleared:
                self.calendar.defer(actor,task)
                self.diagnostics['deferred_paid_crop_establishment']+=1
                return None
        if tile == 'LOCKED':
            return walk(pos, task.point) if pos != task.point else ['PASS']
        # Material is carried before travel; a later successful collection can
        # fund the rest of the same trip without a separate warehouse visit.
        required = 'WHEAT' if op == 'FEED' else 'FERTILIZER' if op == 'FERTILIZE' else (
            item if op == 'PLACE' and item in rules.ANIMALS else None)
        if (pos in SHEDS and day < 29 and (pos != task.point or op == 'PLACE')
                and actor in service_actors(self, day)):
            feed = self._trip_inputs(actor, farm, day, self.cursor.get(actor, 0), 'WHEAT')
            if feed > bag.get('WHEAT', 0) and private['shed'].get('WHEAT', 0):
                return self._supply(actor, 'WHEAT', feed, farm, private)
        if required and not bag.get(required, 0):
            amount = max(1, self._trip_inputs(actor, farm, day, self.cursor.get(actor, 0), required))
            return self._supply(actor, required, amount, farm, private)
        if pos != task.point:
            return walk(pos, task.point)
        if op in ('FEED', 'CARE'):
            if not isinstance(tile, dict) or not tile.get('animal'):
                return ['PASS']
            return [op]
        if op in ('WATER', 'FERTILIZE'):
            if not isinstance(tile, dict) or not tile.get('crop'):
                return ['PASS']
            if tile['crop'] != item:
                return None
            return [op]
        if op == 'PLANT' or op.startswith('BUILD_') or (op == 'PLACE' and item in rules.ANIMALS):
            if isinstance(tile, dict) and tile.get('animal'):
                return None
            if isinstance(tile, dict) and tile.get('crop'):
                crop = rules.CROPS[tile['crop']]
                if tile.get('yield_units') and day - tile['planted_day'] >= crop['first_yield_day']:
                    return ['HARVEST']
                if self.calendar is not None and crop['ongoing'] and day-tile['planted_day'] >= (
                        crop['first_yield_day']+(crop['max_yield']-1)*crop['interval']):
                    return ['DIG']
                return ['PASS']
            expected_structure = rules.ANIMALS[item]['structure'] if op == 'PLACE' and item in rules.ANIMALS else None
            if tile is not None and not (isinstance(tile, dict) and tile.get('kind') == expected_structure):
                return ['DIG']
            if op == 'PLANT':
                # New crops need first watering before the current day ends.
                # Retain one following turn in the actor's establishment visit.
                if 24 - self.last_step % 24 < 2:
                    self.diagnostics['plant_without_first_water_slot'] += 1
                    return None
                return ['PLANT', item] if private['seeds'].get(item, 0) else ['PASS']
            if op == 'PLACE' and tile is None:
                return ['BUILD_' + expected_structure]
        return [op, item, 1] if op == 'PLACE' else [op]

    def _orders(self, farm, private, market, day, hour, observation, rounds=None):
        if int(observation['step']) == 718:
            return rank_sales(final_orders(private),market)
        orders = []

        def issue(op, item=None, quantity=1):
            if len(orders) >= self.cfg['maxMarketOrdersPerTurn']:
                return 0
            if op == 'HIRE':
                cost = rules._hire_cost(farm['hires_today'], self.cfg.get('farmHandCostMult', 1))
                if farm['money'] < cost:
                    return 0
                rules._do_hire(farm, private, 10, self.cfg.get('farmHandCostMult', 1))
                orders.append(['HIRE'])
                return 1
            if op == 'BUY_LAND':
                before = len(farm['unlocked_quadrants'])
                rules._do_buy_land(farm, 10)
                if before == len(farm['unlocked_quadrants']):
                    return 0
                orders.append(['BUY_LAND'])
                return 1
            accepted = 0
            for _ in range(quantity):
                price = rules.CROPS[item]['seed'] if op == 'BUY_SEED' else (
                    rules.ANIMALS[item]['cost'] if op == 'BUY_ANIMAL' else
                    rules.market_price(item, market['inventory'][item] - (op == 'BUY_PRODUCT'), market.get('params')))
                if not rules._commit_unit(op, item, price, farm, private, market, 100):
                    break
                accepted += 1
                if rounds is not None and op in ('SELL', 'BUY_PRODUCT'):
                    rounds.append(((int(observation['player']), op, item, price),))
            if accepted:
                orders.append([op, item, accepted])
            return accepted

        seeds, animals = {}, {}
        reserves = Counter()
        locked = False
        for actor in range(self.programme.workers[day] + 1):
            sequence = self._tasks(day).get(actor, ())
            if self.cursor.get(actor, 0) >= len(sequence):
                continue
            inputs = self._warehouse_inputs(actor, farm, private, day)
            if inputs:
                reserves.update(inputs)
            cached = self._procurement_sequences.get(actor)
            if cached is None or cached[0] is not sequence:
                prefix = [0]
                for previous, task in zip(sequence, sequence[1:]):
                    prefix.append(prefix[-1]+distance(previous.point, task.point)+1)
                entries = tuple((index, task) for index, task in enumerate(sequence)
                    if task.operation in ('PLANT', 'PLACE', 'BUILD_PASTURE', 'BUILD_COOP'))
                cached = self._procurement_sequences[actor] = (sequence, entries, prefix)
            cursor = self.cursor.get(actor, 0)
            if cursor >= len(sequence) or not cached[1]:
                continue
            pos = tuple(rules._farmer_position(farm, actor)) if actor <= len(farm['hands']) else (4, 4)
            entry_cost = distance(pos, sequence[cursor].point)+1-cached[2][cursor]
            for index, task in cached[1]:
                if index < cursor:
                    continue
                eta = entry_cost+cached[2][index]
                tile = self._tile(farm, task)
                if (task.operation == 'PLANT' and day+rules.CROPS[task.item]['first_yield_day']<=29
                        and not self._established(task,tile)):
                    seeds.setdefault((task.point, task.item), eta)
                if task.operation == 'PLACE' and task.item in rules.ANIMALS and not self._established(task,tile):
                    animals.setdefault((task.point, task.item), eta)
                if tile == 'LOCKED' and task.operation in ('PLANT', 'PLACE', 'BUILD_PASTURE', 'BUILD_COOP'):
                    locked = True
        needed_animals = Counter(item for _, item in animals)
        carried = Counter()
        if animals:
            for bag in private['inventories']:
                if bag:
                    carried.update(bag)
        missing_inputs={item:max(0,reserves[item]-private['shed'].get(item,0))
                        for item in ('WHEAT','FERTILIZER')}
        sell = {item:max(0, private['shed'].get(item, 0)-reserves.get(item, 0)) for item in rules.PRODUCTS}
        if self.settlement:
            # Paid productive work and the next day's service budget retain
            # first claim on cash before stored goods receive warehouse space.
            multiplier = self.cfg.get('farmHandCostMult', 1)
            cash_required = _labor_obligation(farm['hires_today'],self.programme.workers[day],
                self.programme.workers[day+1] if day<29 else 0,multiplier)
            cash_required += sum(rules.market_price(item,market['inventory'][item]-i-1,market.get('params'))
                                 for item,quantity in missing_inputs.items() for i in range(quantity))
            for item, count in needed_animals.items():
                cash_required += rules.ANIMALS[item]['cost']*max(0,count-carried[item]-private['shed'].get(item,0))
            cash_required += sum(rules.CROPS[item]['seed'] for _,item in seeds)
            if locked and len(farm['unlocked_quadrants']) < 4:
                cash_required += rules.LAND_PRICES[len(farm['unlocked_quadrants'])-1]
            incoming_purchase_units = sum(max(0,count-carried[item]-private['shed'].get(item,0))
                                          for item,count in needed_animals.items())
            incoming_purchase_units += sum(missing_inputs.values())
            settle = self.settlement if callable(self.settlement) else sale_caps
            sell, record = settle(observation, farm, private, market, reserves, cash_required,
                                     incoming_purchase_units=incoming_purchase_units)
            if record:
                self.settlement_records.append(record)
                self.diagnostics['settlement_hold_steps'] += 1
        for _,item,quantity in rank_sales([['SELL',item,quantity] for item,quantity in sell.items() if quantity],market):
            issue('SELL', item, quantity)
        if hour < 18:
            # The first four hands are inexpensive and provide the financing
            # and material collection tasks that release later work.
            for _ in range(max(0, min(4, self.programme.workers[day]) - farm['hires_today'])):
                if not issue('HIRE'):
                    break
        for item in missing_inputs:
            issue('BUY_PRODUCT',item,max(0,reserves[item]-private['shed'].get(item,0)))
        if locked and hour < 18:
            issue('BUY_LAND')
        for item, count in needed_animals.items():
            issue('BUY_ANIMAL', item, max(0, count - carried[item] - private['shed'].get(item, 0)))
        if hour < 16:
            for _ in range(max(0, self.programme.workers[day] - farm['hires_today'])):
                if not issue('HIRE'):
                    break
        if not seeds:
            return orders
        stock = Counter(private['seeds'])
        for (point, crop), eta in sorted(seeds.items(), key=lambda row: row[1]):
            if stock[crop]:
                stock[crop] -= 1
            elif eta <= min(8, 24 - hour):
                issue('BUY_SEED', crop)
        return orders

    def action(self, observation, *, projected=False, reuse_market=False, owned_projection=False):
        tick = int(observation['step'])
        if self.last_step is not None and tick <= self.last_step:
            raise ValueError('Recipe compiler requires successive observations')
        self.last_step = tick
        day, hour = divmod(tick, 24)
        seat = int(observation['player'])
        if day != self.day:
            if self.calendar is not None:
                self.calendar.begin(day,observation['farms'][seat],self.cursor)
            self.day, self.cursor = day, {}
            self.day_sequences=None
        if owned_projection and not projected:
            raise ValueError('Borrowed tile ownership requires an isolated projection')
        lazy_farm = owned_projection and not callable(self.settlement) and self.survival_admission is None
        farm = (clone_action_farm if lazy_farm else clone_farm)(observation['farms'][seat])
        private = clone_private(observation['private'])
        market = clone_market(observation['market'])
        actions = []
        expiring = None
        unit_errors = [] if projected else None
        unit_harvest = Counter() if projected else None
        for actor in range(len(farm['hands']) + 1):
            revised = fit_first_watering(self, actor, farm, private, tick)
            if revised is not None:
                self.establishment_records.append(revised)
                self.diagnostics['establishment_tours_reordered'] += 1
            sequence = self._tasks(day).get(actor, ())
            cursor = self.cursor.get(actor, 0)
            op = None
            while cursor < len(sequence):
                self.cursor[actor] = cursor
                protected = protect_successor_service(self, actor, farm, private, day, tick)
                if protected is not None:
                    self.deadline_records.append(protected)
                    self.diagnostics['successor_services_protected'] += 1
                    sequence = self._tasks(day).get(actor, ())
                precedence = ensure_establishment_precedence(self, actor, farm, day, tick)
                if precedence is not None:
                    self.establishment_records.append(precedence)
                    self.diagnostics['establishment_dependencies_ordered'] += 1
                    sequence = self._tasks(day).get(actor, ())
                feeding = prioritize_due_feed(self, actor, farm, private, day, tick)
                if feeding is not None:
                    self.deadline_records.append(feeding)
                    self.diagnostics['due_feeds_prioritized'] += 1
                    sequence = self._tasks(day).get(actor, ())
                revised = defer_optional_water(self, actor, farm, day, tick)
                if revised is not None:
                    self.deadline_records.append(revised)
                    self.diagnostics['deadline_water_reorders'] += 1
                    sequence = self._tasks(day).get(actor, ())
                op = self._command(actor, sequence[cursor], farm, private, day)
                if op is not None:
                    break
                cursor += 1
            self.cursor[actor] = cursor
            if op is None:
                assigned=take_paid_visit(self,actor,farm,private,market,tick)
                if assigned is None:
                    assigned = take_critical_water(self, actor, farm, private, market, tick)
                if assigned is None:
                    if expiring is None:
                        expiring = expiring_crops(farm, day)
                    assigned = take_orphan_harvest(self, actor, farm, private, market, tick, expiring)
                if assigned is None:
                    assigned = take_survival_water(self, actor, farm, private, market, tick)
                if assigned is not None:
                    self.dispatch_records.append(assigned)
                    diagnostic = {'orphan_harvest': 'orphan_harvest_visits',
                                  'critical_water': 'critical_water_visits',
                                  'survival_water': 'survival_water_visits'}.get(assigned.get('kind'), 'paid_visits_transferred')
                    self.diagnostics[diagnostic] += 1
                    sequence=self._tasks(day).get(actor,())
                    while cursor<len(sequence):
                        self.cursor[actor]=cursor
                        op=self._command(actor,sequence[cursor],farm,private,day)
                        if op is not None:
                            break
                        cursor+=1
                    self.cursor[actor]=cursor
            op = op or ['PASS']
            op, terminal = last_chance_delivery(tick, farm, private, actor, op, market, self.cfg['shedCapacity'])
            if terminal is not None:
                self.terminal_records.append(terminal)
                self.diagnostics['terminal_delivery_override_steps'] += 1
            pos = tuple(rules._farmer_position(farm, actor))
            tile = farm['tiles'][pos[1]][pos[0]]
            inspect_effect = op[0] not in rules.FARMER_MOVES and op[0] != 'PASS'
            effect_kind=0
            if inspect_effect:
                if lazy_farm and isinstance(tile, dict):
                    tile = dict(tile)
                    farm['tiles'][pos[1]][pos[0]] = tile
                    effect_field=_SERVICE_EFFECT_FIELDS.get(op[0])
                    if effect_field is not None and effect_field in tile:
                        before_effect=tile[effect_field]
                        effect_kind=1
                    elif op[0]=='FERTILIZE':
                        before_effect=private['inventories'][actor].get('FERTILIZER',0)
                        effect_kind=2
                if effect_kind==0:
                    before = (dict(tile) if isinstance(tile,dict) else tile, dict(private['inventories'][actor]), dict(private['seeds']), dict(private['shed']))
            if projected:
                bag=private['inventories'][actor]
                if op==['FEED'] and isinstance(tile,dict) and tile.get('animal') and not tile['fed_today'] and bag.get('WHEAT',0)<1:
                    unit_errors.append(dict(actor=actor,code='FEED_SHORT',op=op,point=list(pos)))
                if op==['FERTILIZE'] and isinstance(tile,dict) and tile.get('crop') and bag.get('FERTILIZER',0)<1:
                    unit_errors.append(dict(actor=actor,code='FERTILIZER_SHORT',op=op,point=list(pos)))
                if op[:1]==['PICKUP'] and pos in SHEDS and private['shed'].get(op[1],0)<(int(op[2]) if len(op)>2 else 1):
                    unit_errors.append(dict(actor=actor,code='PICKUP_SHORT',op=op,point=list(pos)))
            rules._apply_unit_action(farm, private, actor, op, 10, day, 24, 100)
            if op==['HARVEST']:
                if projected:
                    for product,quantity in private['inventories'][actor].items():
                        if quantity>before[1].get(product,0):
                            unit_harvest[product]+=quantity-before[1].get(product,0)
                produced=before[0]
                owner=None
                if isinstance(produced,dict):
                    item=produced.get('animal',produced.get('crop'))
                    born=produced.get('placed_day',produced.get('planted_day'))
                    paid_identity=(pos,item,born)
                    owner=next((lot for lot,binding in self.lot_bindings.items()
                                if binding==paid_identity),None)
                if owner is not None:
                    # A successor's clearance or an orphan pickup still harvests
                    # the actual paid cohort, regardless of the task label.
                    product=rules.ANIMALS[item]['product'] if item in rules.ANIMALS else item
                    quantity=private['inventories'][actor].get(product,0)-before[1].get(product,0)
                    self.lot_harvest[owner]+=max(0,quantity)
                elif cursor<len(sequence) and sequence[cursor].lot:
                    task=sequence[cursor]
                    owned=(self._successor_matches(task,before[0]) if managed(task)
                           else identity(task,before[0]) is not None)
                    if task.operation!='CLEAR_CROP' and task.point==pos and owned:
                        product=rules.ANIMALS[task.item]['product'] if task.item in rules.ANIMALS else task.item
                        quantity=private['inventories'][actor].get(product,0)-before[1].get(product,0)
                        self.lot_harvest[task.lot]+=max(0,quantity)
            if (cursor<len(sequence) and managed(sequence[cursor]) and op[0] in ('PLANT','PLACE')
                    and sequence[cursor].operation==op[0]):
                self._successor_matches(sequence[cursor],farm['tiles'][pos[1]][pos[0]])
            if inspect_effect:
                if effect_kind==1:
                    unchanged=before_effect==tile[effect_field]
                elif effect_kind==2:
                    unchanged=before_effect==private['inventories'][actor].get('FERTILIZER',0)
                else:
                    after = (farm['tiles'][pos[1]][pos[0]], private['inventories'][actor], private['seeds'], private['shed'])
                    unchanged=before==after
                if unchanged:
                    self.diagnostics['no_effect_' + op[0]] += 1
                elif cursor < len(sequence) and op[0] == sequence[cursor].operation:
                    if op[0] not in ('PICKUP', 'DROP', 'PLACE'):
                        self.cursor[actor] += 1
                    if op[0] == 'PLANT':
                        # A paid crop's first watering is part of establishment.
                        # Keep it in both live execution and native continuation quotes.
                        task = sequence[cursor]
                        water = Task(task.point, 'WATER', task.item, task.generation, task.lot)
                        following = self.cursor[actor]
                        if sequence[following:following + 1] != (water,):
                            changed = dict(self._tasks(day))
                            changed[actor] = sequence[:following] + (water,) + sequence[following:]
                            self.day_sequences = changed
                            if self.calendar is not None:
                                self.calendar.sequences = dict(changed)
                            self.diagnostics['first_watering_dependency_inserted'] += 1
            if op == ['PASS'] and cursor < len(sequence):
                self.diagnostics['blocked_' + sequence[cursor].operation] += 1
            self.diagnostics['action_' + op[0]] += 1
            actions.append(op)
        if not projected:
            return dict(farmer=actions[0], hands=actions[1:], market=self._orders(farm, private, market, day, hour, observation))
        requested=Counter(op[1] for op in actions if op[:1]==['PLANT'])
        if any(count>observation['private']['seeds'].get(item,0) for item,count in requested.items()) or callable(self.settlement):
            action=dict(farmer=actions[0],hands=actions[1:],market=self._orders(farm,private,market,day,hour,observation))
            return action,None
        # Order planning owns separate mutable market fields. Unit effects are
        # transferred only to the isolated simulator, never to live observations.
        quote_farm=dict(farm,hands=list(farm['hands']),unlocked_quadrants=list(farm['unlocked_quadrants']),
            tiles=[list(row) for row in farm['tiles']])
        quote_private=dict(private,shed=dict(private['shed']),seeds=dict(private['seeds']),
            inventories=list(private['inventories']))
        rounds=[] if reuse_market and tick<718 else None
        action=dict(farmer=actions[0],hands=actions[1:],
            market=self._orders(quote_farm,quote_private,market,day,hour,observation,rounds))
        if rounds is None:
            return action,(farm,private,unit_errors,unit_harvest)
        if action['market']:
            rules._refresh_prices(market)
        completed_market=(quote_farm,quote_private,market,rounds)
        return action,(farm,private,unit_errors,unit_harvest,completed_market)


_SERVICE_EFFECT_FIELDS = {'WATER':'watered_today','FEED':'fed_today',
    'CARE':'cared_today','COLLECT_FERTILIZER':'fertilizer_available'}


def compile_recipe(observation, configuration, programme, world, *, continuation=None, settlement=False, bind_herd=False, adaptive_crops=False, end_step=719):
    """Charge cyclic collection once to each isolated native continuation quote."""
    effective_settlement = continuation.settlement if continuation is not None else settlement
    if not gc.isenabled() or callable(effective_settlement) or threading.active_count() != 1:
        return _compile_recipe(observation, configuration, programme, world,
            continuation=continuation, settlement=settlement, bind_herd=bind_herd,
            adaptive_crops=adaptive_crops, end_step=end_step)
    gc.disable()
    try:
        return _compile_recipe(observation, configuration, programme, world,
            continuation=continuation, settlement=settlement, bind_herd=bind_herd,
            adaptive_crops=adaptive_crops, end_step=end_step)
    finally:
        counts, thresholds = gc.get_count(), gc.get_threshold()
        generation = (0 if counts[1] < thresholds[1]
                      else 1 if counts[2] < thresholds[2] else 2)
        gc.collect(generation)
        gc.enable()


def _compile_recipe(observation, configuration, programme, world, *, continuation=None, settlement=False, bind_herd=False, adaptive_crops=False, end_step=719):
    if continuation is not None:
        if continuation.programme != programme or continuation.cfg != configuration:
            raise ValueError('A continuation belongs to its own programme and configuration')
        controller = continuation.fork()
    else:
        controller = RecipeCompiler(programme, configuration, settlement=settlement,bind_herd=bind_herd,adaptive_crops=adaptive_crops)
    route = [dict(farmer=['PASS'], hands=[], market=[]) for _ in range(720)]
    model = ScenarioRules(int(observation['player']), int(observation['step']),
                          world.future_shops, world.external_flow, world.scale,consumption=world.additional_consumption,
                          record_units=world.uncertain_shop_demand)

    def choose(own, unused):
        model.prepare(own,configuration)
        if own['step'] in world.public_farms:
            own['farms'][1-int(own['player'])]=clone_farm(world.public_farms[own['step']])
        action,unit_phase = controller.action(own,projected=True,reuse_market=model.can_reuse_own_market(),owned_projection=True)
        route[own['step']] = action
        return action,unit_phase

    result = project(observation, route, configuration, model, fund=False, record=True, transform=choose,end_step=end_step,generated_actions=True,fused_units=True)
    result['end_step'] = end_step
    result['rival_net_cash'] = model.rival_value
    if model.unit_rounds is not None and end_step == 719:
        from .demand_value import value_trade_tape
        result['demand_value']=value_trade_tape(observation,configuration,model.unit_rounds,
            own_cash=result['cash'],rival_cash=model.rival_value)
    result['lot_harvest'] = dict(controller.lot_harvest)
    result['lot_bindings'] = dict(controller.lot_bindings)
    result['allocation_records'] = list(controller.allocation_records)
    result['establishment_records'] = list(controller.establishment_records)
    result['settlement_records'] = list(controller.settlement_records)
    return CompiledProgramme(tuple(route[:719]), result, dict(controller.diagnostics))
