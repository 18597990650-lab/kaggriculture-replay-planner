"""Spatial work and delivery ordering learned from successful service visits."""
from dataclasses import dataclass

from . import rules


@dataclass(frozen=True)
class Visit:
    point: tuple[int,int]
    kind: str
    items: tuple[str,...]
    operations: tuple[str,...]


@dataclass(frozen=True)
class ServiceTours:
    name: str
    days: tuple[dict[int,tuple[Visit,...]],...]

    @classmethod
    def from_dict(cls,data):
        if len(data['days'])!=30:
            raise ValueError('A service prior must cover all days')
        days = []
        for rows in data['days']:
            actors = {}
            for actor,sequence in rows.items():
                if not actor.isdigit() or not 0<=int(actor)<=16:
                    raise ValueError('Invalid demonstrated actor index')
                visits = []
                for row in sequence:
                    point = tuple(row['point'])
                    if len(point)!=2 or any(type(x)is not int or not 0<=x<10 for x in point):
                        raise ValueError('Invalid demonstrated service position')
                    kind = row['kind']
                    if kind not in ('work','deposit'):
                        raise ValueError('Invalid visit kind')
                    items = tuple(row['items']) if kind=='deposit' else ()
                    if kind=='deposit' and (not items or any(item not in rules.PRODUCTS for item in items)):
                        raise ValueError('A deposit prior requires explicit products')
                    operations = tuple(row['operations'])
                    legal = {'DIG','PLANT','WATER','FERTILIZE','HARVEST','BUILD_COOP','BUILD_PASTURE',
                             'PLACE','DROP','FEED','CARE','COLLECT_FERTILIZER'}
                    if not operations or any(op not in legal for op in operations):
                        raise ValueError('Invalid demonstrated work phase')
                    visits.append(Visit(point,kind,items,operations))
                actors[int(actor)] = tuple(visits)
            days.append(actors)
        return cls(data['name'],tuple(days))
