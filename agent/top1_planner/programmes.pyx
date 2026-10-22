"""Production recipes with ordered work, supply and settlement stages."""
from dataclasses import dataclass
from sys import intern

from . import rules


@dataclass(frozen=True)
cdef class Task:
    point: object
    operation: object
    item: object
    generation: object
    lot: object = None

    def __post_init__(self):
        if type(self.operation) is str: self.operation = intern(self.operation)
        if type(self.item) is str: self.item = intern(self.item)
        if type(self.lot) is str: self.lot = intern(self.lot)

    def __getstate__(self):
        return [self.point,self.operation,self.item,self.generation,self.lot]

    def __setstate__(self,state):
        if not isinstance(state,list) or len(state)!=5:
            raise TypeError('Expected the five immutable legacy task fields')
        self.point,self.operation,self.item,self.generation,self.lot=state
        if type(self.operation) is str: self.operation = intern(self.operation)
        if type(self.item) is str: self.item = intern(self.item)
        if type(self.lot) is str: self.lot = intern(self.lot)


@dataclass(frozen=True, slots=True)
class ProductionProgramme:
    name: str
    days: tuple[dict[int, tuple[Task, ...]], ...]
    workers: tuple[int, ...]

    @classmethod
    def from_dict(cls, data):
        if len(data['days']) != 30 or len(data['workers']) != 30:
            raise ValueError('A production programme must cover all thirty days')
        legal = {'DIG', 'PLANT', 'WATER', 'FERTILIZE', 'HARVEST', 'BUILD_COOP',
                 'BUILD_PASTURE', 'PLACE', 'DROP', 'PICKUP', 'FEED', 'CARE',
                 'COLLECT_FERTILIZER', 'DEPOSIT', 'CLEAR_CROP'}
        days = []
        for day, actors in enumerate(data['days']):
            rows = {}
            for actor, sequence in actors.items():
                if not actor.isdigit() or not 0 <= int(actor) <= 16:
                    raise ValueError('Invalid programme actor')
                tasks = []
                for row in sequence:
                    point = tuple(row['point'])
                    if len(point) != 2 or any(type(v) is not int or not 0 <= v < 10 for v in point):
                        raise ValueError('Invalid programme point')
                    op, item, generation = row['operation'], row['item'], row['generation']
                    if op not in legal:
                        raise ValueError('Invalid task operation')
                    if item is not None and item not in {*rules.PRODUCTS, *rules.ANIMALS}:
                        raise ValueError('Invalid task material')
                    if generation is not None and not 0 <= generation <= day:
                        raise ValueError('Invalid production generation')
                    if op in ('PLANT', 'PLACE', 'PICKUP', 'DEPOSIT') and item is None:
                        raise ValueError('A material task requires its item')
                    lot = row.get('lot')
                    if lot is not None and (not isinstance(lot,str) or not lot or len(lot)>100):
                        raise ValueError('Invalid task lot identity')
                    if op == 'CLEAR_CROP' and (item not in rules.CROPS or generation is None):
                        raise ValueError('Crop clearance requires the observed crop identity')
                    tasks.append(Task(point, op, item, generation, lot))
                rows[int(actor)] = tuple(tasks)
            days.append(rows)
        workers = tuple(data['workers'])
        if any(type(n) is not int or not 0 <= n <= 16 for n in workers):
            raise ValueError('Invalid programme labor requirement')
        return cls(data['name'], tuple(days), workers)
