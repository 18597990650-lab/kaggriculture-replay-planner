"""Carry unfinished establishment work across daily labor and biological clocks."""
from . import rules
from .compiler import distance
from .programmes import Task


class WorkCalendar:
    """Keep proposed establishment separate from the observed paid crop state."""

    def __init__(self,programme):
        self.programme=programme
        self.day=None
        self.sequences={}
        self.pending={}

    def fork(self, *, programme=None):
        child=WorkCalendar(self.programme if programme is None else programme)
        child.day=self.day
        child.sequences=dict(self.sequences)
        child.pending=dict(self.pending)
        return child

    def satisfied(self,task,farm):
        tile=farm['tiles'][task.point[1]][task.point[0]]
        if not isinstance(tile,dict):
            return False
        if task.operation=='PLACE':
            if task.lot:
                return tile.get('animal')==task.item and tile.get('placed_day',-1)>=task.generation
            return bool(tile.get('animal'))
        return tile.get('crop')==task.item and tile['planted_day']>=task.generation

    def defer(self,actor,task):
        if task.operation in ('PLANT','PLACE'):
            self.pending[task.point]=(actor,task)

    def visit(self,task):
        old=self.pending.get(task.point)
        if old is not None and old[1]==task:
            self.pending.pop(task.point)

    def begin(self,day,farm,cursors):
        if day==self.day:
            return
        for actor,sequence in self.sequences.items():
            for task in sequence[cursors.get(actor,0):]:
                if task.operation in ('PLANT','PLACE') and not self.satisfied(task,farm):
                    self.defer(actor,task)
        self.day=day
        self.sequences=dict(self.programme.days[day])
        replaced={task.point for sequence in self.sequences.values() for task in sequence
                  if task.operation in ('PLANT','PLACE')}
        self.pending={point:job for point,job in self.pending.items()
                      if point not in replaced and not self.satisfied(job[1],farm)}
        for point,(previous_actor,task) in sorted(self.pending.items()):
            # Establishment remains an atomic visit when its start is delayed.
            bundle=(task,Task(point,'WATER',task.item,task.generation,task.lot)) if task.operation=='PLANT' else (task,)
            choices=[]
            for actor in range(self.programme.workers[day]+1):
                sequence=self.sequences.get(actor,())
                route=((4,4),*(other.point for other in sequence))
                load=len(sequence)+sum(distance(a,b) for a,b in zip(route,route[1:]))
                for index in range(len(sequence)+1):
                    before=route[index]
                    after=sequence[index].point if index<len(sequence) else None
                    if any(other.point==point and other.operation in ('WATER','FERTILIZE','FEED','CARE')
                           for other in sequence[:index]):
                        continue
                    if after==before and not (after==point and (index==0 or sequence[index-1].point!=point)):
                        continue
                    extra=distance(before,point)+len(bundle)
                    if after is not None:
                        extra+=distance(point,after)-distance(before,after)
                    choices.append((max(0,load+extra-24),extra,actor!=previous_actor,load,actor,index))
            *_,actor,index=min(choices)
            sequence=self.sequences.get(actor,())
            self.sequences[actor]=sequence[:index]+bundle+sequence[index:]


def productive_fertilizer_window(tile,day):
    """A three-day fertilizer application must overlap actual crop production."""
    if not isinstance(tile,dict) or tile.get('crop') not in rules.CROPS:
        return False
    if tile.get('fertilized_until_day',-1)>=day+2:
        return False
    data=rules.CROPS[tile['crop']]
    age=day-tile['planted_day']
    if data['ongoing']:
        return any(age+offset+1>=data['first_yield_day']
                   and (age+offset+1-data['first_yield_day'])%data['interval']==0
                   and (age+offset+1-data['first_yield_day'])//data['interval']<data['max_yield']
                   for offset in range(3))
    return tile['yield_units']<data['max_yield'] and any(
        (data['max_yield_day']+1)//2<=age+offset<=data['max_yield_day'] for offset in range(3))
