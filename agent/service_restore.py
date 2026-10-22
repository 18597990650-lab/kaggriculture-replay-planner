"""Offer complete future service again for an observed, deliberately paused herd."""
from dataclasses import replace
import math

from top1_planner.allocation_admission import _comparison
from top1_planner.task_allocation import Revision

_ADMISSION_IDS = set()

from top1_planner.service_cycle import PREFIX


def alternatives(programme, observation):
    tick = int(observation['step'])
    if tick % 24 or not 216 <= tick <= 648:
        return ()
    first = tick // 24
    farm = observation['farms'][int(observation['player'])]
    matches = {}
    for day in range(first, 29):
        for sequence in programme.days[day].values():
            for task in sequence:
                if task.operation not in ('FEED', 'CARE') or not task.lot or not task.lot.startswith(PREFIX):
                    continue
                x, y = task.point
                tile = farm['tiles'][y][x]
                if isinstance(tile, dict) and (tile.get('animal'), tile.get('placed_day')) == (task.item, task.generation):
                    matches.setdefault(task.item, set()).add((task.point, task.generation))
    proposals = []
    for animal in sorted(matches):
        identities = matches[animal]
        days = list(programme.days[:first])
        for day in range(first, 30):
            actors = {}
            for actor, sequence in programme.days[day].items():
                actors[actor] = tuple(
                    replace(task, lot=None) if day < 29 and task.item == animal
                    and (task.point, task.generation) in identities
                    and task.operation in ('FEED', 'CARE') and task.lot and task.lot.startswith(PREFIX)
                    else task for task in sequence)
            days.append(actors)
        proposals.append(replace(programme, name=f'{programme.name}-restore-service-{animal.lower()}-{tick}', days=tuple(days)))
    return tuple(proposals)


def install(runtime):
    original = runtime.paid_herd_continuations

    def proposals(programme, observation, **kwargs):
        previous = original(programme, observation, **kwargs)
        restored = alternatives(programme, observation)
        _ADMISSION_IDS.clear()
        _ADMISSION_IDS.update(id(p) for p in restored)
        if restored:
            runtime._TRACE.append(dict(kind='PAUSED_SERVICE_RESTORATION', step=int(observation['step']),
                proposals=[p.name for p in restored], admission='existing paired physical screen and full settlement',
                changes='restore future feed/care intentions for matching paid animals; preserve task order, workforce and executed prefix'))
        return (*restored, *previous)

    runtime.paid_herd_continuations = proposals


def admission(programme, compiled, baseline, observation):
    if id(programme) not in _ADMISSION_IDS:
        return None
    revision = Revision(programme, dict(kind='restored_service', retired_animals=[],
        added_plantings=[], added_herds=[], lots=[]))
    row = dict(_comparison(compiled, baseline, observation, revision), programme=programme.name)
    minimum = compiled.result['minimum_cash']
    if not math.isfinite(minimum) or minimum < 0:
        row['rejection_reasons'].append('UNFUNDED_SERVICE_RESTORATION')
    row['admitted'] = not row['rejection_reasons']
    return row
