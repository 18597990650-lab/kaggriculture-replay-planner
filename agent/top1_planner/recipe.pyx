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


from .terminal_delivery import last_chance_delivery, final_orders
from .calendar import WorkCalendar, productive_fertilizer_window
from .redeployment import managed, identity
from .crop_input_value import fertilizer_due

from .service_cycle import service_required
from .sale_order import rank_sales
from .programmes cimport Task
from .material_needs import MaterialNeeds
from .orphan_harvest import expiring_crops, take_orphan_harvest
from .critical_water import take_critical_water
from .survival_water import take_survival_water





@lru_cache(maxsize=2048)
def _labor_obligation(hired, today, tomorrow, multiplier):
    return (sum(rules._hire_cost(i, multiplier) for i in range(hired, today))
            + sum(rules._hire_cost(i, multiplier) for i in range(tomorrow)))


cdef class RecipeCompiler:
    """Own a paid continuation; derive moves and quantities from actual resources."""

    cdef dict __dict__
    cdef public object _native_material_needs
    cdef public object _establishment_sequences
    cdef public object _procurement_sequences
    cdef public object _warehouse_sequences
    cdef public object adaptive_crops
    cdef public object allocation_records
    cdef public object bind_herd
    cdef public object calendar
    cdef public object cfg
    cdef public object cursor
    cdef public object day
    cdef public object day_sequences
    cdef public object deadline_records
    cdef public object diagnostics
    cdef public object dispatch_records
    cdef public object establishment_records
    cdef public object last_step
    cdef public object lot_bindings
    cdef public object lot_harvest
    cdef public object programme
    cdef public object repair_checked
    cdef public object settlement
    cdef public object settlement_records
    cdef public object successor_service_actors
    cdef public object successor_service_rows
    cdef public object survival_admission
    cdef public object survival_approved
    cdef public object survival_denied
    cdef public object terminal_records

    def __getstate__(self):
        state=dict(self.__dict__)
        for name in ('_establishment_sequences', '_procurement_sequences', '_warehouse_sequences', 'adaptive_crops', 'allocation_records', 'bind_herd', 'calendar', 'cfg', 'cursor', 'day', 'day_sequences', 'deadline_records', 'diagnostics', 'dispatch_records', 'establishment_records', 'last_step', 'lot_bindings', 'lot_harvest', 'programme', 'repair_checked', 'settlement', 'settlement_records', 'successor_service_actors', 'successor_service_rows', 'survival_admission', 'survival_approved', 'survival_denied', 'terminal_records'):
            state[name]=getattr(self,name)
        return state

    def __setstate__(self,state):
        if not isinstance(state,dict):
            raise TypeError('Controller state must be a dictionary')
        for name,value in state.items():
            setattr(self,name,value)

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
        self._native_material_needs = None

    cpdef object can_continue(self,programme):
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

    cpdef object _tile(self, farm, Task task):
        return farm['tiles'][task.point[1]][task.point[0]]

    cpdef object _successor_matches(self, Task task, tile):
        found=_direct_identity(task,tile)
        actual=(task.point,*found) if found else None
        bound=self.lot_bindings.get(task.lot)
        if bound is not None:
            return actual==bound
        if actual is not None and actual not in self.lot_bindings.values():
            self.lot_bindings[task.lot]=actual
            return True
        return False

    cpdef object _established(self, Task task, tile):
        if _direct_managed(task):
            self._successor_matches(task,tile)
            return task.lot in self.lot_bindings
        if task.operation=='PLACE':
            return isinstance(tile,dict) and (bool(tile.get('animal')) if self.bind_herd else tile.get('animal')==task.item)
        return isinstance(tile,dict) and tile.get('crop')==task.item and tile['planted_day']>=task.generation

    cpdef object _tasks(self,day):
        if day==self.day and self.day_sequences is not None:
            return self.day_sequences
        if self.calendar is not None and self.calendar.day==day:
            return self.calendar.sequences
        return self.programme.days[day]

    cpdef object _remaining(self, actor, day):
        return self._tasks(day).get(actor, ())[self.cursor.get(actor, 0):]

    def _establishment_pending(self, Task task, day):
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

    cpdef object _trip_inputs(self, actor, farm, day, start, wanted=None):
        cdef Task task
        """Required starting stock before the next supply stop, net of own collection."""
        balance, required = Counter(), Counter()
        seen = set()
        closed = set()
        for task in self._tasks(day).get(actor, ())[start:]:
            op, item = task.operation, task.item
            tile = self._tile(farm, task)
            if not _direct_service_required(task, tile, day):
                continue
            if _direct_managed(task) and op in ('FEED','CARE','HARVEST','COLLECT_FERTILIZER','WATER','FERTILIZE') and not self._successor_matches(task,tile) and not self._establishment_pending(task,day):
                continue
            if _direct_managed(task) and op=='FERTILIZE' and not fertilizer_due(tile,day):
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
        cdef Task task
        """Credit only this actor's carried stock and collection before consumption."""
        bag = private['inventories'][actor] if actor < len(private['inventories']) else {}
        balance = {'WHEAT': bag.get('WHEAT', 0), 'FERTILIZER': bag.get('FERTILIZER', 0)}
        required, seen = {}, set()
        sequence = self._tasks(day).get(actor, ())
        cached = self._warehouse_sequences.get(actor)
        if cached is None or cached[0] is not sequence:
            entries = tuple((index, task) for index, task in enumerate(sequence)
                if task.operation in ('FEED', 'FERTILIZE', 'HARVEST', 'COLLECT_FERTILIZER')
                or task.operation in ('CARE', 'WATER') and _direct_managed(task))
            cached = self._warehouse_sequences[actor] = (sequence, entries)
        cursor = self.cursor.get(actor, 0)
        for index, task in cached[1]:
            if index < cursor:
                continue
            tile = self._tile(farm, task)
            op = task.operation
            if not _direct_service_required(task, tile, day):
                continue
            if _direct_managed(task) and op in ('FEED','CARE','HARVEST','COLLECT_FERTILIZER','WATER','FERTILIZE') and not self._successor_matches(task,tile) and not self._establishment_pending(task,day):
                continue
            if _direct_managed(task) and op=='FERTILIZE' and not fertilizer_due(tile,day):
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

    cpdef object _supply(self, actor, item, quantity, farm, private):
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

    def _deposit(self, actor, Task task, farm, private, day):
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

    cpdef object _command(self, actor, Task task, farm, private, day):
        """None means completed or obsolete; PASS retains an unmet dependency."""
        op, item = task.operation, task.item
        tile = self._tile(farm, task)
        if op=='PLANT' and day+rules.CROPS[item]['first_yield_day']>29:
            return None
        if not _direct_service_required(task, tile, day):
            self.diagnostics['cycle_pause_' + op] += 1
            return None
        if self.calendar is not None:
            self.calendar.visit(task)
        if _direct_managed(task):
            if op=='FERTILIZE' and not fertilizer_due(tile,day):
                return None
            if op in ('PLANT','PLACE') and self._established(task,tile):
                return None
            if op in ('WATER','FERTILIZE','HARVEST','FEED','CARE','COLLECT_FERTILIZER') and not self._successor_matches(task,tile):
                return None
        if self.bind_herd and not _direct_managed(task) and op in ('FEED','CARE') and isinstance(tile,dict) and tile.get('animal'):
            item=tile['animal']
        if task.lot and not _direct_managed(task) and op in ('WATER','FERTILIZE','HARVEST') and _direct_identity(task,tile) is None:
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
            if op == 'PLACE' and not _direct_managed(task) and (bool(tile.get('animal')) if self.bind_herd else tile.get('animal') == item):
                return None
            if op == 'PLANT' and not _direct_managed(task) and tile.get('crop') == item and tile['planted_day'] >= task.generation:
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
                and actor in _inline_successor_service_service_actors(self, day)):
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

    def _orders(self, farm, private, market, day, hour, observation, rounds=None, reuse_tiles=False):
        cdef Task task
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

        if self._native_material_needs is None:
            self._native_material_needs=MaterialNeeds()
        seeds,animals,reserves,locked=self._native_material_needs.gather(self,farm,private,day,reuse_tiles)
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
        cdef Task task
        cdef int effect_kind
        cdef bint unchanged
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
            revised = _inline_establishment_order_fit_first_watering(self, actor, farm, private, tick)
            if revised is not None:
                self.establishment_records.append(revised)
                self.diagnostics['establishment_tours_reordered'] += 1
            sequence = self._tasks(day).get(actor, ())
            cursor = self.cursor.get(actor, 0)
            op = None
            while cursor < len(sequence):
                self.cursor[actor] = cursor
                protected = _inline_successor_service_protect_successor_service(self, actor, farm, private, day, tick)
                if protected is not None:
                    self.deadline_records.append(protected)
                    self.diagnostics['successor_services_protected'] += 1
                    sequence = self._tasks(day).get(actor, ())
                precedence = _inline_establishment_precedence_ensure_establishment_precedence(self, actor, farm, day, tick)
                if precedence is not None:
                    self.establishment_records.append(precedence)
                    self.diagnostics['establishment_dependencies_ordered'] += 1
                    sequence = self._tasks(day).get(actor, ())
                feeding = _inline_due_feed_order_prioritize_due_feed(self, actor, farm, private, day, tick)
                if feeding is not None:
                    self.deadline_records.append(feeding)
                    self.diagnostics['due_feeds_prioritized'] += 1
                    sequence = self._tasks(day).get(actor, ())
                revised = _inline_deadline_order_defer_optional_water(self, actor, farm, day, tick)
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
                if lazy_farm:
                    if self._native_material_needs is None:
                        self._native_material_needs=MaterialNeeds()
                    assigned=self._native_material_needs.paid_visit(self,actor,farm,private,market,tick,True)
                else:
                    assigned=_inline_work_dispatch_take_paid_visit(self,actor,farm,private,market,tick)
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
                    owned=(self._successor_matches(task,before[0]) if _direct_managed(task)
                           else _direct_identity(task,before[0]) is not None)
                    if task.operation!='CLEAR_CROP' and task.point==pos and owned:
                        product=rules.ANIMALS[task.item]['product'] if task.item in rules.ANIMALS else task.item
                        quantity=private['inventories'][actor].get(product,0)-before[1].get(product,0)
                        self.lot_harvest[task.lot]+=max(0,quantity)
            if (cursor<len(sequence) and _direct_managed(sequence[cursor]) and op[0] in ('PLANT','PLACE')
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
            market=self._orders(quote_farm,quote_private,market,day,hour,observation,rounds,reuse_tiles=lazy_farm))
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


_inline_work_dispatch_SERVICE={'WATER','FERTILIZE','HARVEST','FEED','CARE','COLLECT_FERTILIZER'}

cdef object _inline_work_dispatch__establishment_blocks(RecipeCompiler controller,task,farm):
    """Completed retry instructions do not reserve an already productive plot."""
    tile=farm['tiles'][task.point[1]][task.point[0]]
    if task.operation in ('PLANT','PLACE'):
        return not controller._established(task,tile)
    if task.operation=='CLEAR_CROP':
        return isinstance(tile,dict) and tile.get('crop')==task.item and tile.get('planted_day')==task.generation
    if task.operation=='DIG':
        return tile is not None and not (isinstance(tile,dict) and tile.get('animal'))
    if task.operation in ('BUILD_PASTURE','BUILD_COOP'):
        return not (isinstance(tile,dict) and tile.get('kind')==task.operation[6:])
    return False

cdef object _inline_work_dispatch__ready(task,tile,day):
    if not isinstance(tile,dict):
        return False
    if not _direct_service_required(task,tile,day):
        return False
    if task.lot and _direct_identity(task,tile) is None:
        return False
    op=task.operation
    if op=='HARVEST':
        return bool(tile.get('yield_units')) and (bool(tile.get('animal')) or
            tile.get('crop') in rules.CROPS and day-tile['planted_day']>=rules.CROPS[tile['crop']]['first_yield_day'])
    if op=='WATER':
        return bool(tile.get('crop')) and not tile.get('watered_today')
    if op=='FERTILIZE':
        return bool(tile.get('crop')) and tile.get('fertilized_until_day',-1)<day+2
    if op=='FEED':
        return bool(tile.get('animal')) and not tile.get('fed_today')
    if op=='CARE':
        return bool(tile.get('animal')) and not tile.get('cared_today')
    return op=='COLLECT_FERTILIZER' and bool(tile.get('fertilizer_available'))

cdef object _inline_work_dispatch_take_paid_visit(RecipeCompiler controller,actor,farm,private,market,tick):
    cdef Task task
    """Borrow one complete ready visit only after the recipient's queue is empty.

    Every task remains owned by one queue. Seeds, animal placement, clearance,
    warehouse operations and the executed prefix are not transferable. Required
    feed and fertilizer must already be in the recipient's bag. Collection that
    the donor needs for a later use in the same trip remains with that donor.
    """
    day,hour=divmod(tick,24)
    rows=controller._tasks(day)
    recipient=rows.get(actor,())
    cursor=controller.cursor.get(actor,0)
    if cursor<len(recipient):
        return None
    available=min(24-hour,719-tick)
    if available<2:
        return None
    pos=tuple(rules._farmer_position(farm,actor))
    bag=private['inventories'][actor]
    successor_points={t.point for sequence in rows.values() for t in sequence
        if _direct_managed(t) and t.operation in _inline_work_dispatch_SERVICE|{'PLANT','PLACE'}}
    establishing={t.point for owner,sequence in rows.items()
        for t in sequence[controller.cursor.get(owner,0):]
        if t.operation in ('PLANT','PLACE','DIG','CLEAR_CROP','BUILD_PASTURE','BUILD_COOP')
        and (t.point not in successor_points or _inline_work_dispatch__establishment_blocks(controller,t,farm))}
    candidates=[]
    for donor,sequence in rows.items():
        if donor==actor or donor>len(farm['hands']):
            continue
        start=controller.cursor.get(donor,0)
        previous=tuple(rules._farmer_position(farm,donor))
        eta=0
        index=start
        while index<len(sequence):
            task=sequence[index]
            point=task.point
            if task.operation not in _inline_work_dispatch_SERVICE:
                eta+=distance(previous,point)+1
                previous=point
                index+=1
                continue
            end=index+1
            while end<len(sequence) and sequence[end].point==point and sequence[end].operation in _inline_work_dispatch_SERVICE:
                end+=1
            block=sequence[index:end]
            tile=farm['tiles'][point[1]][point[0]]
            ready=[t for t in block if _inline_work_dispatch__ready(t,tile,day)
                and (not _direct_managed(t) or controller._successor_matches(t,tile))]
            travel=distance(previous,point)
            eta+=travel+len(ready)
            previous=point
            if not ready or point in establishing:
                index=end
                continue
            inputs=Counter('WHEAT' if t.operation=='FEED' else 'FERTILIZER'
                for t in ready if t.operation in ('FEED','FERTILIZE'))
            if any(bag.get(item,0)<qty for item,qty in inputs.items()):
                index=end
                continue
            product=rules.ANIMALS[tile['animal']]['product'] if tile.get('animal') else tile['crop']
            collected=Counter()
            if any(t.operation=='COLLECT_FERTILIZER' for t in ready):
                collected['FERTILIZER']+=1
            if any(t.operation=='HARVEST' for t in ready):
                collected[product]+=tile.get('yield_units',0)
            needed=controller._trip_inputs(donor,farm,day,end)
            if any(needed[item]>private['inventories'][donor].get(item,0)
                   for item in ('WHEAT','FERTILIZER') if collected[item]):
                index=end
                continue
            finish=distance(pos,point)+len(ready)
            delivery=distance(point,nearest_shed(point))+len(collected) if collected else 0
            if finish+delivery>available or finish>=eta:
                index=end
                continue
            # Prices rank completion opportunities; the full native policy
            # evaluation, rather than this local ranking, values final profit.
            value=sum(market['prices'][item]*qty for item,qty in collected.items())
            if any(t.operation in ('FEED','CARE','WATER') for t in ready):
                units=rules.ANIMALS[tile['animal']]['max_held'] if tile.get('animal') else rules.CROPS[product]['max_yield']
                value+=market['prices'][product]*units
            candidates.append((value/max(1,finish+delivery),eta-finish,-finish,donor,index,end,block,collected))
            index=end
    if not candidates:
        return None
    _,saved,negative_finish,donor,start,end,block,collected=max(candidates,key=lambda row:row[:6])
    changed=dict(rows)
    changed[donor]=rows[donor][:start]+rows[donor][end:]
    delivery=tuple(Task(nearest_shed(block[0].point),'DEPOSIT',item,None)
        for item in collected if collected[item])
    changed[actor]=recipient+block+delivery
    controller.day_sequences=changed
    if controller.calendar is not None:
        controller.calendar.sequences=dict(changed)
    return dict(step=tick,donor=donor,recipient=actor,point=block[0].point,
        operations=[t.operation for t in block],lots=[t.lot for t in block],
        estimated_steps_earlier=saved,estimated_finish=tick-negative_finish,
        carried_inputs=dict(bag),extra_hires=0,new_establishment=0)

cdef object _inline_successor_service_service_actors(RecipeCompiler controller, day):
    rows = controller._tasks(day)
    if rows is not controller.successor_service_rows:
        controller.successor_service_rows = rows
        controller.successor_service_actors = frozenset(actor for actor, sequence in rows.items()
            if any(_direct_managed(t) and t.operation in ('FEED', 'CARE') for t in sequence))
    return controller.successor_service_actors

cdef object _inline_successor_service_protect_successor_service(RecipeCompiler controller, actor, farm, private, day, tick):
    if day >= 29 or actor not in _inline_successor_service_service_actors(controller, day):
        return None
    rows = controller._tasks(day)
    sequence = rows.get(actor, ())
    cursor = controller.cursor.get(actor, 0)
    remaining = sequence[cursor:]
    if not remaining:
        return None
    pos = tuple(rules._farmer_position(farm, actor))
    bag = private['inventories'][actor]
    available = min(24 - tick % 24, 719 - tick)
    lots = {}
    for index in range(cursor, len(sequence)):
        task = sequence[index]
        if not _direct_managed(task) or task.operation not in ('FEED', 'CARE'):
            continue
        tile = farm['tiles'][task.point[1]][task.point[0]]
        if not controller._successor_matches(task, tile) or not _direct_service_required(task, tile, day):
            continue
        fulfilled = tile['fed_today'] if task.operation == 'FEED' else tile['cared_today']
        if fulfilled:
            continue
        lots.setdefault(task.lot, []).append((index, task))
    candidates = []
    for lot, pairs in lots.items():
        pairs.sort(key=lambda pair: (pair[1].operation != 'FEED', pair[0]))
        point = pairs[0][1].point
        travel = distance(pos, point)
        needs_feed = any(t.operation == 'FEED' for _, t in pairs)
        if needs_feed and not bag.get('WHEAT', 0):
            if not private['shed'].get('WHEAT', 0):
                continue
            shed = nearest_shed(pos)
            travel = distance(pos, shed) + 1 + distance(shed, point)
        finish = travel + len(pairs)
        if finish + 1 < available or finish > available:
            continue
        if tuple(t for _, t in pairs) == remaining[:len(pairs)]:
            continue
        candidates.append((available - finish, pairs[0][0], lot, pairs))
    if not candidates:
        return None
    _, _, lot, pairs = min(candidates)
    indices = {index for index, _ in pairs}
    advanced = tuple(task for _, task in pairs)
    changed = dict(rows)
    changed[actor] = sequence[:cursor] + advanced + tuple(
        task for index, task in enumerate(sequence[cursor:], cursor) if index not in indices)
    controller.day_sequences = changed
    if controller.calendar is not None:
        controller.calendar.sequences = dict(changed)
    return dict(kind='successor_service_deadline', step=tick, actor=actor, lot=lot,
        point=advanced[0].point, operations=[t.operation for t in advanced],
        added_tasks=0, extra_hires=0, paid_prefix_preserved=True)


cdef inline bint _direct_managed(Task task):
    return bool(task.lot and task.lot.startswith('redeploy:'))

cdef inline object _direct_identity(Task task, tile):
    """A successor service task cannot bind to the animal or crop it displaced."""
    if not isinstance(tile, dict):
        return None
    key, generation = ('animal', 'placed_day') if task.item in rules.ANIMALS else ('crop', 'planted_day')
    if tile.get(key) == task.item and tile.get(generation, -1) >= task.generation:
        return task.item, tile[generation]
    return None

cdef inline bint _direct_service_required(Task task, tile, day):
    """A quoted pause is revoked when actual hunger makes recovery necessary."""
    if not task.lot or not task.lot.startswith('cycle-pause:') or task.operation not in ('FEED', 'CARE'):
        return True
    if not isinstance(tile, dict) or (tile.get('animal'), tile.get('placed_day')) != (task.item, task.generation):
        return True
    return day < 29 and tile['consecutive_unfed'] >= 1


cdef object _inline_establishment_order_fit_first_watering(RecipeCompiler controller, actor, farm, private, tick):
    cdef Task plant, water
    """Reorder independent planting visits only when their present tour overruns.

    Each visit retains its seed and same-site first watering. Paid plots,
    material collection, service tours and already executed tasks are excluded.
    The small subset dynamic programme minimizes travel; successful execution
    still depends on actual seed procurement and land availability.
    """
    day, hour = divmod(tick, 24)
    rows = controller._tasks(day)
    sequence = rows.get(actor, ())
    if controller.cursor.get(actor, 0) or private['inventories'][actor]:
        return None
    if not 4 <= len(sequence) <= 16 or len(sequence) % 2:
        return None
    visits, points, work = [], set(), 0
    for index in range(0, len(sequence), 2):
        plant = sequence[index]
        water = sequence[index + 1]
        if (plant.operation != 'PLANT' or water.operation != 'WATER'
                or plant.item not in rules.CROPS or plant.generation != day
                or (plant.point, plant.item, plant.generation, plant.lot)
                != (water.point, water.item, water.generation, water.lot)
                or plant.point in points):
            return None
        tile = farm['tiles'][plant.point[1]][plant.point[0]]
        if tile is not None and tile != 'LOCKED' and not (
                isinstance(tile, dict) and tile.get('kind') == 'WEED'):
            return None
        points.add(plant.point)
        visits.append((plant, water))
        work += 2 + int(isinstance(tile, dict) and tile.get('kind') == 'WEED')
    origin = tuple(rules._farmer_position(farm, actor))
    positions = [pair[0].point for pair in visits]
    original = distance(origin, positions[0]) + sum(
        distance(a, b) for a, b in zip(positions, positions[1:]))
    available = min(24 - hour, 719 - tick)
    if original + work <= available:
        return None
    count = len(visits)
    paths = {(1 << index, index): (distance(origin, point), (index,))
             for index, point in enumerate(positions)}
    for mask in range(1, 1 << count):
        for last in range(count):
            prefix = paths.get((mask, last))
            if prefix is None:
                continue
            cost, order = prefix
            for nxt in range(count):
                if mask & (1 << nxt):
                    continue
                key = (mask | (1 << nxt), nxt)
                value = (cost + distance(positions[last], positions[nxt]), order + (nxt,))
                if key not in paths or value < paths[key]:
                    paths[key] = value
    best, order = min(paths[((1 << count) - 1, last)] for last in range(count))
    if best + work > available or best >= original:
        return None
    changed = dict(rows)
    changed[actor] = tuple(task for index in order for task in visits[index])
    controller.day_sequences = changed
    if controller.calendar is not None:
        controller.calendar.sequences = dict(changed)
    return dict(step=tick, actor=actor, origin=origin,
                before=[list(point) for point in positions],
                after=[list(positions[index]) for index in order],
                previous_steps=original + work, revised_steps=best + work,
                available_steps=available, extra_hires=0, extra_inputs=0,
                scope='Unstarted planting and first-water pairs; no paid-prefix change or execution guarantee')

cdef object _inline_establishment_precedence_ensure_establishment_precedence(RecipeCompiler controller, actor, farm, day, tick):
    cdef Task service, task
    rows = controller._tasks(day)
    sequence = rows.get(actor, ())
    start = controller.cursor.get(actor, 0)
    if start >= len(sequence):
        return None
    service = sequence[start]
    if service.operation not in ('FEED', 'CARE', 'WATER', 'FERTILIZE'):
        return None
    point = service.point
    tile = farm['tiles'][point[1]][point[0]]
    if tile is not None and (not isinstance(tile, dict) or tile.get('crop') or tile.get('animal')):
        return None
    expected = 'PLACE' if service.item in rules.ANIMALS else 'PLANT'
    end_service = start
    while end_service < len(sequence):
        task = sequence[end_service]
        if (task.point != point or task.operation not in ('FEED', 'CARE', 'WATER', 'FERTILIZE', 'HARVEST', 'COLLECT_FERTILIZER')
                or task.item != service.item or task.generation != service.generation or task.lot != service.lot):
            break
        end_service += 1
    for end in range(end_service, len(sequence)):
        task = sequence[end]
        if task.point != point or task.operation not in ('DIG', 'BUILD_COOP', 'BUILD_PASTURE', 'PLANT', 'PLACE'):
            return None
        if task.operation != expected:
            continue
        if task.item != service.item or task.generation != service.generation or task.lot != service.lot:
            return None
        establishment = sequence[end_service:end + 1]
        dependent = sequence[start:end_service]
        if not dependent:
            return None
        changed = dict(rows)
        changed[actor] = sequence[:start] + establishment + dependent + sequence[end + 1:]
        controller.day_sequences = changed
        if controller.calendar is not None:
            controller.calendar.sequences = dict(changed)
        return dict(kind='establishment_precedence', step=tick, actor=actor, point=point,
                    item=service.item, generation=service.generation, cursor=start,
                    establishment=[task.operation for task in establishment],
                    deferred_service=[task.operation for task in dependent],
                    added_tasks=0, extra_hires=0, paid_prefix_preserved=True)
    return None

cdef object _inline_due_feed_order_prioritize_due_feed(RecipeCompiler controller, actor, farm, private, day, tick):
    cdef Task current, task
    if day >= 29 or tick % 24 != 23:
        return None
    if private['inventories'][actor].get('WHEAT', 0) < 1:
        return None
    rows = controller._tasks(day)
    sequence = rows.get(actor, ())
    start = controller.cursor.get(actor, 0)
    if start >= len(sequence):
        return None
    current = sequence[start]
    if current.operation not in ('HARVEST', 'CARE', 'COLLECT_FERTILIZER'):
        return None
    if tuple(rules._farmer_position(farm, actor)) != current.point:
        return None
    tile = farm['tiles'][current.point[1]][current.point[0]]
    if not (isinstance(tile, dict) and tile.get('animal') == current.item
            and tile.get('placed_day') == current.generation
            and not tile['fed_today'] and tile['consecutive_unfed'] >= 1):
        return None
    for index in range(start + 1, len(sequence)):
        task = sequence[index]
        if (task.point != current.point or task.item != current.item
                or task.generation != current.generation or task.lot != current.lot
                or task.operation not in ('HARVEST', 'FEED', 'CARE', 'COLLECT_FERTILIZER')):
            return None
        if task.operation != 'FEED':
            continue
        if not _direct_service_required(task, tile, day):
            return None
        changed = dict(rows)
        changed[actor] = sequence[:start] + (task,) + sequence[start:index] + sequence[index + 1:]
        controller.day_sequences = changed
        if controller.calendar is not None:
            controller.calendar.sequences = dict(changed)
        return dict(kind='due_feed_order', step=tick, actor=actor, point=task.point,
                    item=task.item, generation=task.generation, advanced='FEED',
                    deferred=current.operation, carried_wheat=private['inventories'][actor]['WHEAT'],
                    added_tasks=0, extra_hires=0, new_material_purchases=0)
    return None

cdef object _inline_deadline_order__harvest_instead(tile, day, tick):
    crop = rules.CROPS[tile['crop']]
    ready = not crop['ongoing'] and tile['yield_units'] > 0 and day - tile['planted_day'] >= crop['first_yield_day']
    complete = tile['yield_units'] >= crop['max_yield'] or day - tile['planted_day'] > crop['max_yield_day']
    last = (tick + 1) % 24 == 0 and tick + 1 >= tile['max_lifespan_step'] >= 0
    return ready and (complete or last)

cdef object _inline_deadline_order__water_times(RecipeCompiler controller, sequence, position, farm, day, tick):
    cdef Task task
    """Estimate optimistic travel and crop work without waits or supply stops.

    This screens for deadline risk, not complete feasibility or economic
    value. The native continuation remains responsible for those.
    """
    tiles = {}
    elapsed = 0
    visits = {}
    for task in sequence:
        point, op, item = task.point, task.operation, task.item
        if point not in tiles:
            original = farm['tiles'][point[1]][point[0]]
            tiles[point] = dict(original) if isinstance(original, dict) else original
        tile = tiles[point]
        plant = isinstance(tile, dict) and tile.get('crop') in rules.CROPS
        animal = isinstance(tile, dict) and bool(tile.get('animal'))
        if op in ('DROP', 'DEPOSIT', 'PICKUP'):
            continue
        if task.lot and op in ('WATER', 'FERTILIZE', 'HARVEST') and _direct_identity(task,tile) is None:
            continue
        if op == 'PLANT':
            if animal or (plant and tile['crop'] == item and tile['planted_day'] >= task.generation):
                continue
            if plant and controller.adaptive_crops:
                crop = rules.CROPS[tile['crop']]
                age = day - tile['planted_day']
                ready = tile['yield_units'] and age >= crop['first_yield_day']
                cleared = crop['ongoing'] and age >= crop['first_yield_day'] + (crop['max_yield'] - 1) * crop['interval'] and not tile['yield_units']
                if not ready and not cleared:
                    continue
            tiles[point] = rules._new_plant(item, day, 24)
        elif op in ('DIG', 'CLEAR_CROP'):
            if tile is None or animal:
                continue
            if op == 'CLEAR_CROP' and not (plant and tile['crop'] == item and tile['planted_day'] == task.generation):
                continue
            tiles[point] = None
        elif op == 'WATER':
            if not plant or tile['watered_today'] or (tile['crop'] != item and not controller.adaptive_crops):
                continue
            if _inline_deadline_order__harvest_instead(tile, day, tick + elapsed + distance(position, point)):
                tiles[point] = None
            else:
                if tile['consecutive_unwatered'] >= 1:
                    key = (point, tile['crop'], tile['planted_day'])
                    visits[key] = elapsed + distance(position, point) + 1
                tile['watered_today'] = True
        elif op == 'HARVEST':
            if not (plant or animal) or not tile.get('yield_units'):
                continue
            if plant and day - tile['planted_day'] < rules.CROPS[tile['crop']]['first_yield_day']:
                continue
            if plant and not rules.CROPS[tile['crop']]['ongoing']:
                tiles[point] = None
            else:
                tile['yield_units'] = 0
        elif op == 'FERTILIZE':
            if not plant or tile.get('fertilized_until_day', -1) >= day + 2:
                continue
            # Omitting optional fertilizer work keeps this travel estimate optimistic.
            continue
        elif op in ('FEED', 'CARE', 'COLLECT_FERTILIZER'):
            if not animal:
                continue
            flag = {'FEED': 'fed_today', 'CARE': 'cared_today', 'COLLECT_FERTILIZER': 'fertilizer_available'}[op]
            needed = tile.get(flag, False) if op == 'COLLECT_FERTILIZER' else not tile.get(flag, False)
            if not needed:
                continue
            tile[flag] = op != 'COLLECT_FERTILIZER'
        else:
            # Establishment dependencies outside crop work need a full native quote.
            return None
        elapsed += distance(position, point) + 1
        position = point
    return visits

cdef object _inline_deadline_order_defer_optional_water(RecipeCompiler controller, actor, farm, day, tick):
    cdef Task task
    rows = controller._tasks(day)
    sequence = rows.get(actor, ())
    cursor = controller.cursor.get(actor, 0)
    if cursor >= len(sequence):
        return None
    task = sequence[cursor]
    # Inserted lots may yield their optional service; inherited tours retain
    # their established crop and material ordering.
    if task.operation != 'WATER' or not task.lot:
        return None
    tile = farm['tiles'][task.point[1]][task.point[0]]
    if not (isinstance(tile, dict) and tile.get('crop') in rules.CROPS):
        return None
    if tile['watered_today'] or tile['consecutive_unwatered'] >= 1:
        return None
    if task.lot and not (tile['crop'] == task.item and tile['planted_day'] >= task.generation):
        return None
    if tile['crop'] != task.item and not controller.adaptive_crops:
        return None
    if _inline_deadline_order__harvest_instead(tile, day, tick) or 0 <= tile['max_lifespan_step'] <= (day + 1) * 24:
        return None
    # Keep same-site harvest, fertilizer, establishment and material dependencies together.
    if any(other.point == task.point for owner, tasks in rows.items()
           for index, other in enumerate(tasks)
           if index >= controller.cursor.get(owner, 0) and (owner != actor or index != cursor)):
        return None
    available = min(24 - tick % 24, 719 - tick)
    if available <= 1:
        return None
    pos = tuple(rules._farmer_position(farm, actor))
    before = _inline_deadline_order__water_times(controller, sequence[cursor:], pos, farm, day, tick)
    if before is None:
        return None
    missed = {key for key, eta in before.items() if eta > available}
    if not missed:
        return None
    suffix = sequence[cursor + 1:] + (task,)
    after = _inline_deadline_order__water_times(controller, suffix, pos, farm, day, tick)
    if after is None or before.keys() != after.keys():
        return None
    new_missed = {key for key, eta in after.items() if eta > available}
    if not new_missed < missed:
        return None
    changed = dict(rows)
    changed[actor] = sequence[:cursor] + suffix
    controller.day_sequences = changed
    if controller.calendar is not None:
        controller.calendar.sequences = dict(changed)
    return dict(step=tick, actor=actor, deferred_point=task.point, item=tile['crop'], lot=task.lot,
        available_steps=available,
        recovered=[dict(point=key[0], item=key[1], generation=key[2],
                        earlier_eta=before[key], revised_eta=after[key]) for key in sorted(missed - new_missed)],
        scope='Optimistic own-queue deadline ordering; no new inputs, ownership transfer or profit guarantee')
