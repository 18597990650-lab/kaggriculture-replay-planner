"""Reprice executable crop work through the remaining harvest and sale horizon."""
import time

from .allocation_admission import _available_group,_comparison,revision_patch
from .available_crop_admission import cash_value,quote_summary
from .recipe import compile_recipe
from .task_allocation import allocation_variants,apply_revision,QuotePrices
from .redeployment import redeployment_variants
from .rotation_projects import rotation_variants
from .input_projects import input_variant
from .service_cycle import cycle_variant
from .crop_calendar import calendar_variants
from . import quote_timing


def reallocate_current(observation,configuration,controller,world,seconds):
    start=time.perf_counter()
    deadline=start+seconds
    tick=int(observation['step'])
    remaining=719-tick
    estimate=quote_timing.estimate(tick,True)
    record=dict(kind='CURRENT_PLAN_REALLOCATION',step=tick,changed=False,
        allocated_seconds=seconds,results=[],screen=[],proposals=[])
    if 2*estimate+.1>seconds:
        record.update(reason='No complete paired continuation fits',seconds=0)
        return controller,record

    def quote(programme,settlement):
        before=time.perf_counter()
        result=compile_recipe(observation,configuration,programme,world,
            continuation=controller.fork(programme=programme,settlement=settlement),
            settlement=settlement,bind_herd=controller.bind_herd,
            adaptive_crops=controller.adaptive_crops)
        quote_timing.observe(tick,settlement,time.perf_counter()-before)
        return result

    before=time.perf_counter()
    baseline_compiled=quote(controller.programme,True)
    estimate=quote_timing.estimate(tick,True)
    baseline=quote_summary(baseline_compiled.result,observation)
    price=QuotePrices(observation,baseline_compiled)
    proposals=list(allocation_variants(controller.programme,observation,controller,
        price=price,day_states=baseline_compiled.result['day_states'],limit=4))
    proposals.extend(redeployment_variants(controller.programme,observation,controller,
        price=price,day_states=baseline_compiled.result['day_states'],limit=2))
    proposals.extend(rotation_variants(controller.programme,observation,controller,price=price))
    proposals.extend(calendar_variants(controller.programme,observation,controller,price=price,
        day_states=baseline_compiled.result['day_states'])[:1])
    for proposal in (input_variant(controller.programme,observation,controller,price=price,
                                  day_states=baseline_compiled.result['day_states']),
                     cycle_variant(controller.programme,observation,controller,price=price)):
        if proposal is not None:
            proposals.append(proposal)
    group=_available_group(controller.programme,observation,controller)
    if group is not None:
        proposals.append(group)
    proposals.sort(key=lambda r:(-r.detail['estimate'],r.programme.name))
    record['proposals']=[dict(name=r.programme.name,item=r.detail['item'],point=r.detail['point'],
        estimate=r.detail['estimate'],wages=r.detail['marginal_wages']) for r in proposals]
    screen_estimate=quote_timing.estimate(tick,False)
    screened=[]
    for revision in proposals:
        if time.perf_counter()+screen_estimate+2*estimate*1.25+.10>deadline:
            break
        before=time.perf_counter()
        result=quote(revision.programme,False)
        screen_estimate=quote_timing.estimate(tick,False)
        own,rival=cash_value(result.result)
        value=own-rival
        screened.append((value,revision))
    record['screen']=[dict(programme=r.programme.name,relative=value) for value,r in screened]
    selected=([r for value,r in sorted(screened,key=lambda row:(-row[0],row[1].programme.name))]
        if screened else proposals[:1])
    winner=None
    for revision in selected:
        if time.perf_counter()+estimate*1.15+.05>deadline:
            break
        before=time.perf_counter()
        compiled=quote(revision.programme,True)
        estimate=quote_timing.estimate(tick,True)
        row=_comparison(compiled,baseline,observation,revision)
        row.update(programme=revision.programme.name,completed_before_deadline=time.perf_counter()<=deadline)
        row['admitted']=bool(row['admitted'] and row['completed_before_deadline'])
        record['results'].append(row)
        if row['admitted'] and (winner is None or row['relative']>winner[1]['relative']):
            winner=revision,row
    if winner is not None:
        revision,row=winner
        record.update(changed=True,selected=revision.programme.name,
            revision=revision_patch(controller.programme,revision))
        controller=apply_revision(controller,revision)
    record.update(seconds=time.perf_counter()-start,
        reason='Complete late production accepted' if winner else 'Keep the current complete continuation')
    return controller,record
