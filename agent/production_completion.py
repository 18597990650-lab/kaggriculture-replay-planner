"""Complete deferred sowing and placement within the next existing work visits."""
from dataclasses import replace
import math

from top1_planner import rules
from top1_planner.programmes import Task
from herd_completion import continuation as herd_continuation
from top1_planner.allocation_admission import _comparison
from top1_planner.task_allocation import Revision

_PROPOSALS={}


def crop_continuation(programme, observation, grace=2):
    if grace not in (1,2):
        raise ValueError('A completion window must be one or two service days')
    tick=int(observation['step'])
    if tick%24 or not 144<=tick<=576:
        return None
    today=tick//24
    farm=observation['farms'][int(observation['player'])]
    planned={}
    for day in range(today,min(30,today+3)):
        for sequence in programme.days[day].values():
            for task in sequence:
                if task.operation!='PLANT' or task.item not in rules.CROPS or task.lot:
                    continue
                tile=farm['tiles'][task.point[1]][task.point[0]]
                if isinstance(tile,dict) and (tile.get('animal') or tile.get('crop')):
                    continue
                planned.setdefault((task.point,task.item,task.generation),day)
    if not planned:
        return None
    days=list(programme.days);inserted=[]
    for day in range(today,min(29,today+3+grace)):
        rows=dict(programme.days[day]);additions={}
        for (point,crop,generation),start in sorted(planned.items()):
            interval=min(grace,rules.CROPS[crop]['first_yield_day']-1)
            if not start<day<=start+interval:
                continue
            if day+rules.CROPS[crop]['first_yield_day']>29:
                continue
            if any(t.point==point and t.operation in ('PLANT','PLACE','BUILD_COOP','BUILD_PASTURE')
                   for seq in rows.values() for t in seq):
                continue
            visits=[(i,actor) for actor,seq in rows.items() for i,t in enumerate(seq)
                    if t.point==point and t.item==crop and t.generation==generation and t.operation=='WATER']
            if not visits:
                continue
            index,actor=min(visits)
            additions.setdefault(actor,[]).append((index,point,crop,generation))
        for actor,jobs in additions.items():
            sequence=list(rows[actor])
            for index,point,crop,generation in sorted(jobs,reverse=True):
                sequence.insert(index,Task(point,'PLANT',crop,generation))
                inserted.append(dict(day=day,actor=actor,point=point,crop=crop,generation=generation))
            rows[actor]=tuple(sequence)
        days[day]=rows
    if not inserted:
        return None
    candidate=replace(programme,name=programme.name+f'-complete-crop-{tick}-grace{grace}',days=tuple(days))
    assert candidate.days[:today]==programme.days[:today] and candidate.workers==programme.workers
    return candidate,dict(kind='near_term_crop_completion',step=tick,grace_days=grace,visits=inserted,
        basis='Retry unestablished sowing before its next already planned watering; no occupied entry plot is taken over.')


def complete(programme,observation):
    herd=herd_continuation(programme,observation)
    parent=herd[0] if herd else programme
    crop=crop_continuation(parent,observation)
    if not herd and not crop:
        return None
    return crop[0] if crop else parent


def install(search,runtime):
    original=search.unpaid_herd_variants
    prior_admission=search.service_admission

    def variants(programme,observation,**kwargs):
        ordinary=original(programme,observation,**kwargs)
        extra=[]
        _PROPOSALS.clear()
        # Complete newly proposed investments before comparison. The incumbent
        # keeps its established service and sowing sequence.
        for parent in ordinary:
            candidate=complete(parent,observation)
            if candidate is not None:
                extra.append(candidate)
                _PROPOSALS[id(candidate)]=parent.name
        if extra:
            runtime._TRACE.append(dict(kind='COMPLETE_PRODUCTION_CANDIDATES',step=int(observation['step']),
                proposals=[p.name for p in extra],grace_days=2,scope='new_unpaid_herd_portfolio',
                admission='Existing full physical candidate comparison; prefix, workload and already owned entry crops retained'))
        return (*ordinary,*extra)

    def admission(programme,compiled,baseline,observation):
        if id(programme) not in _PROPOSALS:
            return prior_admission(programme,compiled,baseline,observation)
        revision=Revision(programme,dict(kind='production_completion',retired_animals=[],
            added_plantings=[],added_herds=[],lots=[]))
        row=dict(_comparison(compiled,baseline,observation,revision),programme=programme.name,
            kind='production_completion',parent=_PROPOSALS[id(programme)])
        minimum=compiled.result['minimum_cash']
        if not math.isfinite(minimum) or minimum<0:
            row['rejection_reasons'].append('UNFUNDED_PRODUCTION_COMPLETION')
        row['admitted']=not row['rejection_reasons']
        return row

    search.unpaid_herd_variants=variants
    search.service_admission=admission
