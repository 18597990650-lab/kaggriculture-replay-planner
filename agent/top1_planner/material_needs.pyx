"""Native material and procurement scan over persistent immutable task arrays."""
from libc.stdlib cimport calloc, free
from libc.string cimport memset
from libc.stdint cimport uint32_t
from collections import Counter

from . import rules
from .programmes cimport Task
from .redeployment import identity as reference_identity

_ITEMS = tuple(rules.PRODUCTS) + tuple(rules.ANIMALS) + (None,)
_ITEM_INDEX = {item: index for index, item in enumerate(_ITEMS)}
_OPERATIONS = ('FEED', 'FERTILIZE', 'HARVEST', 'COLLECT_FERTILIZER', 'CARE',
               'WATER', 'PLANT', 'PLACE', 'BUILD_PASTURE', 'BUILD_COOP', 'DIG', 'CLEAR_CROP')
_OP_INDEX = {op: index for index, op in enumerate(_OPERATIONS)}
_KINDS = {'PLANT':0,'COOP':1,'PASTURE':2,'WEED':3}

cdef enum:
    FEED = 0
    FERTILIZE = 1
    HARVEST = 2
    COLLECT = 3
    CARE = 4
    WATER = 5
    PLANT = 6
    PLACE = 7
    PASTURE = 8
    COOP = 9
    DIG = 10
    CLEAR = 11

cdef int FIRST[5]
cdef int MAXDAY[5]
cdef int LIMIT[5]
cdef int INTERVAL[5]
cdef bint ONGOING[5]
for _i, _item in enumerate(tuple(rules.CROPS)):
    FIRST[_i] = rules.CROPS[_item]['first_yield_day']
    MAXDAY[_i] = rules.CROPS[_item]['max_yield_day']
    LIMIT[_i] = rules.CROPS[_item]['max_yield']
    INTERVAL[_i] = rules.CROPS[_item]['interval']
    ONGOING[_i] = rules.CROPS[_item]['ongoing']


cdef struct Work:
    int point
    int x
    int y
    int operation
    int item
    int generation
    int travel_prefix
    bint managed
    bint has_lot
    bint pause
    bint warehouse
    bint procurement


cdef struct Tile:
    bint mapping
    bint locked
    int kind
    bint has_animal
    int crop
    int animal
    int planted
    int placed
    int fertilized
    int unfed
    bint fed
    bint cared
    bint watered
    bint fertilizer_available
    double yield_units


cdef inline int _distance(int x1, int y1, int x2, int y2) noexcept:
    cdef int dx=x1-x2, dy=y1-y2
    return (dx if dx>=0 else -dx) + (dy if dy>=0 else -dy)


cdef class NeedSequence:
    cdef object source
    cdef Work* work
    cdef Py_ssize_t count
    cdef bint has_procurement

    def __cinit__(self):
        self.work = NULL
        self.count = 0

    def __init__(self, source):
        cdef Py_ssize_t index
        cdef Task task
        cdef Work* t
        self.source = source
        self.count = len(source)
        if self.count:
            self.work = <Work*>calloc(self.count, sizeof(Work))
            if self.work == NULL:
                raise MemoryError()
        for index in range(self.count):
            task = source[index]
            t = &self.work[index]
            t.x, t.y = task.point
            if not (0<=t.x<10 and 0<=t.y<10):
                raise ValueError('Material task point is outside the supported board')
            t.point = t.y*10+t.x
            t.operation = _OP_INDEX.get(task.operation, -1)
            t.item = _ITEM_INDEX[task.item]
            t.generation = 30 if task.generation is None else task.generation
            if not 0<=t.generation<=30:
                raise ValueError('Material task generation is outside the season')
            t.managed = bool(task.lot and task.lot.startswith('redeploy:'))
            t.has_lot = bool(task.lot)
            t.pause = bool(task.lot and task.lot.startswith('cycle-pause:')
                           and t.operation in (FEED, CARE))
            t.warehouse = (0<=t.operation<=COLLECT or t.managed and t.operation in (CARE,WATER))
            t.procurement = PLANT<=t.operation<=COOP
            self.has_procurement |= t.procurement
            if index:
                t.travel_prefix = (self.work[index-1].travel_prefix
                    + _distance(self.work[index-1].x,self.work[index-1].y,t.x,t.y)+1)

    def __dealloc__(self):
        free(self.work)


cdef class MaterialNeeds:
    """Reuse encoded queues and private-projection tiles across successive steps.

    Queue tuples and Task records are immutable. In an owned projection, actor
    writes replace tile dictionaries; only plant yield can mutate in place
    during same-day decay. A day change reloads all biological fields. Ordinary
    callers decode their current tiles on every gather.
    """
    cdef object sequences
    cdef object tile_sources
    cdef object active_rows
    cdef object farm
    cdef object controller
    cdef Tile tiles[100]
    cdef int tile_days[100]
    cdef uint32_t tile_epochs[100]
    cdef uint32_t seen[18600]
    cdef unsigned char pending[40300]
    cdef uint32_t epoch
    cdef uint32_t seen_epoch
    cdef int day
    cdef bint adaptive
    cdef bint bind_herd
    cdef bint reuse_tiles

    def __cinit__(self):
        self.sequences={}
        self.tile_sources=[None]*100
        memset(self.tile_days, -1, sizeof(self.tile_days))

    def __reduce__(self):
        # Encoded state is only a computational cache, never paid policy state.
        return MaterialNeeds, ()

    cdef NeedSequence _sequence(self, actor, source):
        cdef NeedSequence cached = self.sequences.get(actor)
        if cached is None or cached.source is not source:
            cached = NeedSequence(source)
            self.sequences[actor] = cached
        return cached

    cdef Tile* _tile(self, int point) except NULL:
        cdef object source
        cdef Tile* t=&self.tiles[point]
        if self.tile_epochs[point] == self.epoch:
            return t
        source=self.farm['tiles'][point//10][point%10]
        if (self.reuse_tiles and self.tile_sources[point] is source
                and self.tile_days[point]==self.day):
            if t.mapping:
                t.yield_units=source.get('yield_units',0)
        else:
            memset(t,0,sizeof(Tile))
            t.crop=-1
            t.animal=-1
            t.planted=-1
            t.placed=-1
            t.fertilized=-1
            t.kind=-1
            t.mapping=isinstance(source,dict)
            t.locked=source=='LOCKED'
            if t.mapping:
                t.kind=_KINDS.get(source.get('kind'),-1)
                t.crop=_ITEM_INDEX.get(source.get('crop'),-1)
                t.animal=_ITEM_INDEX.get(source.get('animal'),-1)
                t.has_animal=bool(source.get('animal'))
                t.planted=source.get('planted_day',-1)
                t.placed=source.get('placed_day',-1)
                t.fertilized=source.get('fertilized_until_day',-1)
                t.unfed=source.get('consecutive_unfed',0)
                t.fed=bool(source.get('fed_today'))
                t.cared=bool(source.get('cared_today'))
                t.watered=bool(source.get('watered_today'))
                t.fertilizer_available=bool(source.get('fertilizer_available'))
                t.yield_units=source.get('yield_units',0)
            self.tile_sources[point]=source
            self.tile_days[point]=self.day
        self.tile_epochs[point]=self.epoch
        return t

    cdef inline bint _service_required(self, Work* task, Tile* tile) noexcept:
        if not task.pause or not tile.mapping or tile.animal!=task.item or tile.placed!=task.generation:
            return True
        return self.day<29 and tile.unfed>=1

    cdef inline bint _establishment_pending(self, Work* task) noexcept:
        if not self.pending[task.point*403+task.item*31+task.generation]:
            return False
        if 9<=task.item<=11:
            return True
        return 0<=task.item<5 and self.day+FIRST[task.item]<=29

    cdef bint _matches(self, NeedSequence sequence, Py_ssize_t index, Work* task):
        return self.controller._successor_matches(sequence.source[index], self.tile_sources[task.point])

    cdef bint _established(self, NeedSequence sequence, Py_ssize_t index, Work* task, Tile* tile):
        if task.managed:
            return self.controller._established(sequence.source[index],self.tile_sources[task.point])
        if task.operation==PLACE:
            return tile.mapping and (tile.has_animal if self.bind_herd else tile.animal==task.item)
        if task.generation==30 and tile.mapping and tile.crop==task.item:
            return self.controller._established(sequence.source[index],self.tile_sources[task.point])
        return tile.mapping and tile.crop==task.item and tile.planted>=task.generation

    cdef inline bint _fertilizer_due(self, Tile* tile) noexcept:
        cdef int p=tile.crop, age, first
        if not tile.mapping or not 0<=p<5 or tile.fertilized>=self.day:
            return False
        age=self.day-tile.planted
        if ONGOING[p]:
            first=age+1-FIRST[p]
            return self.day<29 and first>=0 and first%INTERVAL[p]==0 and first//INTERVAL[p]<LIMIT[p]
        if tile.watered:
            return False
        return tile.yield_units+1<LIMIT[p] and (MAXDAY[p]+1)//2<=age<=MAXDAY[p]

    cdef inline bint _productive_window(self, Tile* tile) noexcept:
        cdef int p=tile.crop, age, offset, elapsed
        if not tile.mapping or not 0<=p<5 or tile.fertilized>=self.day+2:
            return False
        age=self.day-tile.planted
        for offset in range(3):
            if ONGOING[p]:
                elapsed=age+offset+1-FIRST[p]
                if elapsed>=0 and elapsed%INTERVAL[p]==0 and elapsed//INTERVAL[p]<LIMIT[p]:
                    return True
            elif tile.yield_units<LIMIT[p] and (MAXDAY[p]+1)//2<=age+offset<=MAXDAY[p]:
                return True
        return False

    cdef bint _identity(self, NeedSequence sequence, Py_ssize_t index, Work* task, Tile* tile):
        if not tile.mapping:
            return False
        if task.generation==30:
            return reference_identity(sequence.source[index],self.tile_sources[task.point]) is not None
        if 9<=task.item<=11:
            return tile.animal==task.item and tile.placed>=task.generation
        return tile.crop==task.item and tile.planted>=task.generation

    cdef bint _ready(self, NeedSequence sequence, Py_ssize_t index, Work* task, Tile* tile):
        if not tile.mapping or not self._service_required(task,tile):
            return False
        if task.has_lot and not self._identity(sequence,index,task,tile):
            return False
        if task.operation==HARVEST:
            return bool(tile.yield_units) and (tile.has_animal or
                0<=tile.crop<5 and self.day-tile.planted>=FIRST[tile.crop])
        if task.operation==WATER:
            return 0<=tile.crop<5 and not tile.watered
        if task.operation==FERTILIZE:
            return 0<=tile.crop<5 and tile.fertilized<self.day+2
        if task.operation==FEED:
            return tile.has_animal and not tile.fed
        if task.operation==CARE:
            return tile.has_animal and not tile.cared
        return task.operation==COLLECT and tile.fertilizer_available

    cdef bint _establishment_blocks(self, NeedSequence sequence, Py_ssize_t index, Work* task, Tile* tile):
        if task.operation in (PLANT,PLACE):
            return not self._established(sequence,index,task,tile)
        if task.operation==CLEAR:
            return tile.mapping and tile.crop==task.item and tile.planted==task.generation
        if task.operation==DIG:
            return self.tile_sources[task.point] is not None and not (tile.mapping and tile.has_animal)
        if task.operation==PASTURE:
            return not (tile.mapping and tile.kind==2)
        if task.operation==COOP:
            return not (tile.mapping and tile.kind==1)
        return False

    def paid_visit(self,controller,int actor,farm,private,market,int tick,bint reuse_tiles=False):
        try:
            return self._paid_visit(controller,actor,farm,private,market,tick,reuse_tiles)
        finally:
            self.controller=None
            self.farm=None

    cdef object _paid_visit(self,controller,int actor,farm,private,market,int tick,bint reuse_tiles):
        cdef int day=tick//24, hour=tick%24, available, donor, owner, cursor, eta
        cdef int posx,posy,prevx,prevy,finish,delivery,feed_need,fertilizer_need,ready_count
        cdef Py_ssize_t index,end,k,start,length
        cdef unsigned char successors[100]
        cdef unsigned char establishing[100]
        cdef bint collect,harvest,productive
        cdef Work* task
        cdef Work* visited
        cdef Tile* tile
        cdef NeedSequence sequence
        cdef object rows=controller._tasks(day), recipient=rows.get(actor,())
        cdef object bag,source,product,collected,needed,value,key,best_key=None,best=None,block
        if not 0<=day<30:
            raise ValueError('Paid visit requires a season tick')
        cursor=controller.cursor.get(actor,0)
        if cursor<len(recipient):
            return None
        available=min(24-hour,719-tick)
        if available<2:
            return None
        self.epoch+=1
        if self.epoch==0:
            memset(self.tile_epochs,0,sizeof(self.tile_epochs))
            self.epoch=1
        self.controller=controller
        self.farm=farm
        self.day=day
        self.adaptive=bool(controller.adaptive_crops)
        self.bind_herd=bool(controller.bind_herd)
        self.reuse_tiles=reuse_tiles
        self.active_rows=rows
        posx,posy=rules._farmer_position(farm,actor)
        bag=private['inventories'][actor]
        memset(successors,0,sizeof(successors))
        memset(establishing,0,sizeof(establishing))
        for owner,source in rows.items():
            sequence=self._sequence(owner,source)
            for index in range(sequence.count):
                task=&sequence.work[index]
                if task.managed and 0<=task.operation<=PLACE:
                    successors[task.point]=1
        for owner,source in rows.items():
            sequence=self._sequence(owner,source)
            start=controller.cursor.get(owner,0)
            if start<0:
                start=max(0,sequence.count+start)
            for index in range(start,sequence.count):
                task=&sequence.work[index]
                if PLANT<=task.operation<=CLEAR:
                    if not successors[task.point]:
                        establishing[task.point]=1
                    elif self._establishment_blocks(sequence,index,task,self._tile(task.point)):
                        establishing[task.point]=1
        for donor,source in rows.items():
            if donor==actor or donor>len(farm['hands']):
                continue
            sequence=self._sequence(donor,source)
            index=controller.cursor.get(donor,0)
            if index<0:
                raise ValueError('Paid visit requires nonnegative execution cursors')
            prevx,prevy=rules._farmer_position(farm,donor)
            eta=0
            while index<sequence.count:
                task=&sequence.work[index]
                if not 0<=task.operation<=WATER:
                    eta+=_distance(prevx,prevy,task.x,task.y)+1
                    prevx,prevy=task.x,task.y
                    index+=1
                    continue
                end=index+1
                while end<sequence.count and sequence.work[end].point==task.point and 0<=sequence.work[end].operation<=WATER:
                    end+=1
                tile=self._tile(task.point)
                ready_count=0
                feed_need=0
                fertilizer_need=0
                collect=False
                harvest=False
                productive=False
                for k in range(index,end):
                    visited=&sequence.work[k]
                    if self._ready(sequence,k,visited,tile) and (not visited.managed or self._matches(sequence,k,visited)):
                        ready_count+=1
                        feed_need+=visited.operation==FEED
                        fertilizer_need+=visited.operation==FERTILIZE
                        collect|=visited.operation==COLLECT
                        harvest|=visited.operation==HARVEST
                        productive|=visited.operation in (FEED,CARE,WATER)
                eta+=_distance(prevx,prevy,task.x,task.y)+ready_count
                prevx,prevy=task.x,task.y
                if ready_count==0 or establishing[task.point]:
                    index=end
                    continue
                if (feed_need and bag.get('WHEAT',0)<feed_need
                        or fertilizer_need and bag.get('FERTILIZER',0)<fertilizer_need):
                    index=end
                    continue
                product=rules.ANIMALS[_ITEMS[tile.animal]]['product'] if tile.has_animal else _ITEMS[tile.crop]
                collected=Counter()
                if collect:
                    collected['FERTILIZER']+=1
                if harvest:
                    collected[product]+=self.tile_sources[task.point].get('yield_units',0)
                needed=controller._trip_inputs(donor,farm,day,end)
                if any(needed[item]>private['inventories'][donor].get(item,0)
                       for item in ('WHEAT','FERTILIZER') if collected[item]):
                    index=end
                    continue
                finish=_distance(posx,posy,task.x,task.y)+ready_count
                delivery=(_distance(task.x,task.y,4 if task.x<5 else 5,4 if task.y<5 else 5)+len(collected)) if collected else 0
                if finish+delivery>available or finish>=eta:
                    index=end
                    continue
                value=sum(market['prices'][item]*qty for item,qty in collected.items())
                if productive:
                    units=rules.ANIMALS[_ITEMS[tile.animal]]['max_held'] if tile.has_animal else rules.CROPS[product]['max_yield']
                    value+=market['prices'][product]*units
                key=(value/max(1,finish+delivery),eta-finish,-finish,donor,index,end)
                if best_key is None or key>best_key:
                    best_key=key
                    best=(donor,index,end,source[index:end],collected)
                index=end
        if best is None:
            return None
        donor,start,end,block,collected=best
        changed=dict(rows)
        changed[donor]=rows[donor][:start]+rows[donor][end:]
        point=block[0].point
        shed=(4 if point[0]<5 else 5,4 if point[1]<5 else 5)
        deposits=tuple(Task(shed,'DEPOSIT',item,None) for item in collected if collected[item])
        changed[actor]=recipient+block+deposits
        controller.day_sequences=changed
        if controller.calendar is not None:
            controller.calendar.sequences=dict(changed)
        return dict(step=tick,donor=donor,recipient=actor,point=point,
            operations=[t.operation for t in block],lots=[t.lot for t in block],
            estimated_steps_earlier=best_key[1],estimated_finish=tick-best_key[2],
            carried_inputs=dict(bag),extra_hires=0,new_establishment=0)

    cdef object _warehouse(self, NeedSequence sequence, int cursor, object bag):
        cdef Py_ssize_t index
        cdef Work* task
        cdef Tile* tile
        cdef int material, key, order0=-1
        cdef int need[2]
        cdef double balance[2]
        cdef bint wants
        balance[0]=bag.get('WHEAT',0)
        balance[1]=bag.get('FERTILIZER',0)
        need[0]=0
        need[1]=0
        self.seen_epoch+=1
        if self.seen_epoch==0:
            memset(self.seen,0,sizeof(self.seen))
            self.seen_epoch=1
        for index in range(max(0,cursor),sequence.count):
            task=&sequence.work[index]
            if not task.warehouse:
                continue
            tile=self._tile(task.point)
            if not self._service_required(task,tile):
                continue
            if task.managed and not self._matches(sequence,index,task) and not self._establishment_pending(task):
                continue
            if task.managed and task.operation==FERTILIZE and not self._fertilizer_due(tile):
                continue
            key=task.point*186+task.operation*31+task.generation
            if self.seen[key]==self.seen_epoch:
                continue
            self.seen[key]=self.seen_epoch
            if task.operation==COLLECT and tile.mapping and tile.fertilizer_available:
                balance[1]+=1
            if task.operation==HARVEST and tile.mapping and tile.crop==0 and self.day-tile.planted>=FIRST[0]:
                balance[0]+=tile.yield_units
            material=-1
            if task.operation==FEED and not (tile.mapping and tile.fed):
                if (tile.mapping and (tile.has_animal if self.bind_herd else tile.animal==task.item)) or self._establishment_pending(task):
                    material=0
            if task.operation==FERTILIZE:
                wants=(self._productive_window(tile) if self.adaptive else
                    tile.mapping and tile.crop==task.item and tile.fertilized<self.day+2)
                if wants:
                    material=1
            if material>=0:
                if balance[material]>0:
                    balance[material]-=1
                else:
                    if order0<0:
                        order0=material
                    need[material]+=1
        if order0==0:
            return {'WHEAT':need[0], 'FERTILIZER':need[1]} if need[1] else {'WHEAT':need[0]}
        if order0==1:
            return {'FERTILIZER':need[1], 'WHEAT':need[0]} if need[0] else {'FERTILIZER':need[1]}
        return {}

    def gather(self, controller, farm, private, int day, bint reuse_tiles=False):
        try:
            return self._gather(controller, farm, private, day, reuse_tiles)
        finally:
            self.controller=None
            self.farm=None

    cdef object _gather(self, controller, farm, private, int day, bint reuse_tiles):
        cdef int actor, cursor, workers, posx, posy, entry_cost, eta
        cdef Py_ssize_t index
        cdef Work* task
        cdef Tile* tile
        cdef NeedSequence sequence
        cdef object rows, source, bag, inputs
        cdef object seeds={}, animals={}, reserves=Counter()
        cdef bint locked=False
        if not 0<=day<30:
            raise ValueError('Material scan requires a season day')
        self.epoch+=1
        if self.epoch==0:
            memset(self.tile_epochs,0,sizeof(self.tile_epochs))
            self.epoch=1
        self.controller=controller
        self.farm=farm
        self.day=day
        self.adaptive=bool(controller.adaptive_crops)
        self.bind_herd=bool(controller.bind_herd)
        self.reuse_tiles=reuse_tiles
        rows=controller._tasks(day)
        self.active_rows=rows
        memset(self.pending,0,sizeof(self.pending))
        for actor,source in rows.items():
            sequence=self._sequence(actor,source)
            cursor=controller.cursor.get(actor,0)
            for index in range(max(0,cursor),sequence.count):
                task=&sequence.work[index]
                if (task.operation==PLACE and 9<=task.item<=11
                        or task.operation==PLANT and task.item<9):
                    self.pending[task.point*403+task.item*31+task.generation]=1
        workers=controller.programme.workers[day]
        for actor in range(workers+1):
            source=rows.get(actor,())
            sequence=self._sequence(actor,source)
            cursor=controller.cursor.get(actor,0)
            if cursor>=sequence.count:
                continue
            bag=private['inventories'][actor] if actor<len(private['inventories']) else {}
            inputs=self._warehouse(sequence,cursor,bag)
            if inputs:
                reserves.update(inputs)
            if not sequence.has_procurement:
                continue
            if actor<=len(farm['hands']):
                posx,posy=rules._farmer_position(farm,actor)
            else:
                posx,posy=4,4
            index=cursor if cursor>=0 else sequence.count+cursor
            if not 0<=index<sequence.count:
                raise IndexError('Material cursor lies outside its task queue')
            task=&sequence.work[index]
            entry_cost=_distance(posx,posy,task.x,task.y)+1-task.travel_prefix
            for index in range(max(0,cursor),sequence.count):
                task=&sequence.work[index]
                if not task.procurement:
                    continue
                eta=entry_cost+task.travel_prefix
                tile=self._tile(task.point)
                if task.operation==PLANT:
                    if not 0<=task.item<5:
                        raise KeyError(_ITEMS[task.item])
                    if self.day+FIRST[task.item]<=29 and not self._established(sequence,index,task,tile):
                        seeds.setdefault(((task.x,task.y),_ITEMS[task.item]),eta)
                if task.operation==PLACE and 9<=task.item<=11 and not self._established(sequence,index,task,tile):
                    animals.setdefault(((task.x,task.y),_ITEMS[task.item]),eta)
                if tile.locked:
                    locked=True
        self.controller=None
        self.farm=None
        return seeds,animals,reserves,locked
