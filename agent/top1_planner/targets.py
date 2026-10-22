"""Production intents learned from complete farms, independent of actor routes."""
from dataclasses import dataclass

from . import rules


@dataclass(frozen=True)
class Target:
    point: tuple[int, int]
    item: str
    generation: int

    @property
    def key(self):
        return self.point, self.item, self.generation


@dataclass(frozen=True)
class ProductionTargets:
    name: str
    days: tuple[tuple[Target, ...], ...]
    daily_workers: tuple[int, ...]

    @classmethod
    def from_dict(cls, data):
        if len(data['days']) != 30 or len(data['daily_workers']) != 30:
            raise ValueError('A production target must cover all 30 days')
        days = []
        for index, rows in enumerate(data['days']):
            points = set()
            day = []
            for row in rows:
                point = tuple(row['point'])
                item = row['item']
                if len(point) != 2 or any(type(x) is not int or not 0 <= x < 10 for x in point):
                    raise ValueError('Invalid production position')
                if point in points or item not in {*rules.CROPS, *rules.ANIMALS}:
                    raise ValueError('Duplicate position or invalid production item')
                if not 0 <= row['generation'] <= index:
                    raise ValueError('A target cannot claim a future paid generation')
                points.add(point)
                day.append(Target(point, item, row['generation']))
            days.append(tuple(day))
        workers = tuple(data['daily_workers'])
        if any(type(n) is not int or not 0 <= n <= 16 for n in workers):
            raise ValueError('Invalid daily labor target')
        return cls(data['name'], tuple(days), workers)

    def at(self, day):
        return {target.point: target for target in self.days[day]}
