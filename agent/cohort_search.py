"""Compare a small funded cohort frontier after the native decision completes."""
import math
import time

import search_policy
from cohort_response import build
from cohort_obligations import same_day_establishments
from cohort_proposals import proposals
from p110_current_world import intraday_world
from top1_planner import quote_timing
from top1_planner.allocation_admission import _comparison
from top1_planner.available_crop_admission import quote_summary


def ordinary_response(observation, configuration, controller, *, free_seconds, shared_remaining, worlds):
    tick = int(observation['step'])
    if tick % 72 != 1 or not 217 <= tick <= 481:
        return controller, None
    started = time.perf_counter()
    audit = dict(kind='DEFERRED_COHORT_RESPONSE', step=tick, parent=controller.programme.name,
        changed=False, selected=controller.programme.name, proposals=[], results=[])
    def retain(reason):
        audit.update(reason=reason, seconds=time.perf_counter()-started)
        return controller, audit
    estimate = quote_timing.estimate(tick, True)
    required = 5 * estimate * 1.2 + .25
    available = max(0., free_seconds) + max(0., shared_remaining-1.)
    if required > available:
        return retain('Insufficient shared budget for a fresh incumbent and bounded cohort frontier')
    deadline = started + required
    audit.update(budget_seconds=required, free_seconds=free_seconds, shared_remaining=shared_remaining)
    world, audit['p110_intraday'] = intraday_world(observation, worlds(observation,future_model='uniform_mean')[1])
    def quote(plan):
        before = time.perf_counter()
        result = search_policy.compile_recipe(observation, configuration, plan, world, settlement=True,
            continuation=controller.fork(programme=plan,settlement=True),
            bind_herd=controller.bind_herd, adaptive_crops=controller.adaptive_crops)
        quote_timing.observe(tick,True,time.perf_counter()-before)
        return result
    baseline = quote(controller.programme)
    summary = quote_summary(baseline.result, observation)
    promised = same_day_establishments(observation, baseline.result, dict(day_states=[]))
    first = tick//24 + bool(promised)
    audit['protected_same_day_establishments'] = promised
    audit['construction_day'] = first
    _, choices = proposals(controller, observation, baseline)
    winner = None
    for choice in choices[:4]:
        if time.perf_counter() + quote_timing.estimate(tick,True)*1.2 + .15 > deadline:
            break
        revision = build(controller,observation,(choice['points'],),allow_hire=True,
            day_states=baseline.result['day_states'], start_day=first)
        audit['proposals'].append(dict(programme=revision.programme.name,**choice))
        if not controller.can_continue(revision.programme):
            raise ValueError('The response changed an executed task prefix')
        compiled = quote(revision.programme)
        row = dict(_comparison(compiled,summary,observation,revision),
            programme=revision.programme.name, points=choice['points'],
            completed_before_deadline=time.perf_counter()<=deadline,
            minimum_cash=compiled.result['minimum_cash'])
        if not row['completed_before_deadline']:
            row['rejection_reasons'].append('INCOMPLETE_WITHIN_BUDGET')
            row['admitted']=False
        if not math.isfinite(row['minimum_cash']) or row['minimum_cash']<0:
            row['rejection_reasons'].append('UNFUNDED_RESPONSE')
            row['admitted']=False
        missing = same_day_establishments(observation, baseline.result, compiled.result)
        row['lost_same_day_establishments'] = missing
        if missing:
            row['rejection_reasons'].append('UNDECLARED_CURRENT_ESTABLISHMENT_LOSS')
            row['admitted'] = False
        audit['results'].append(row)
        if row['admitted'] and (winner is None or row['relative']>winner[1]['relative']):
            winner = revision, row
    if winner is None:
        return retain('Retain native decision; no completed funded cohort improves its same-state tail')
    revision, row = winner
    audit.update(changed=True,selected=revision.programme.name,seconds=time.perf_counter()-started,
        chosen=row,reason='A complete current-state cohort improves the native selected continuation')
    return controller.fork(programme=revision.programme,settlement=True), audit
