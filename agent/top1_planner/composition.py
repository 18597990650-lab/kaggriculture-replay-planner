"""Combine disjoint service pauses with an unchanged paid production prefix."""
from dataclasses import replace

from .task_allocation import Revision


def compose_service(parent, first, second, controller):
    """Return a proposal only; the caller must re-quote its complete cash flow."""
    service = [r for r in (first, second) if r.detail.get('mode')=='guarded_cycle_service']
    if len(service)!=1:
        return None
    pause = service[0]
    production = second if pause is first else first
    if not parent.workers==pause.programme.workers==production.programme.workers:
        return None
    affected = {tuple(p['point']) for p in pause.detail['service_plans']}
    replaced = {tuple(p['point']) for key in ('retired_animals','added_plantings','added_herds')
                for p in production.detail.get(key, ())}
    if affected & replaced:
        return None
    days = []
    for day, actors in enumerate(parent.days):
        a, b = pause.programme.days[day], production.programme.days[day]
        if actors.keys()!=a.keys() or actors.keys()!=b.keys():
            return None
        merged = {}
        for actor, sequence in actors.items():
            if not len(sequence)==len(a[actor])==len(b[actor]):
                return None
            tasks = []
            for original, service_task, production_task in zip(sequence,a[actor],b[actor]):
                if service_task!=original:
                    if (original.operation not in ('FEED','CARE') or
                            replace(service_task,item=original.item,generation=original.generation,
                                    lot=original.lot)!=original or
                            not service_task.lot.startswith('cycle-pause:')):
                        return None
                    if production_task not in (original,service_task):
                        return None
                tasks.append(service_task if service_task!=original else production_task)
            merged[actor] = tuple(tasks)
        days.append(merged)
    target = replace(production.programme, days=tuple(days),
        name=production.programme.name+f'-with-cycle-{pause.detail["step"]}')
    if target.days in (first.programme.days,second.programme.days) or not controller.can_continue(target):
        return None
    detail = dict(production.detail, service_plans=pause.detail['service_plans'],
        composition=[production.programme.name,pause.programme.name],
        service_estimate=pause.detail['estimate'],
        estimate=production.detail['estimate']+pause.detail['estimate'])
    return Revision(target, detail)
