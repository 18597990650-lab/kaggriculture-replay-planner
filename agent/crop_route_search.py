"""Expand a physically quoted crop response under the existing four-quote cap."""
import math
import time

from cohort_search import ordinary_response as herd_response
from cohort_obligations import same_day_establishments
from crop_route_response import variants,eligible_points
from staggered_crop_response import build
from p110_current_world import intraday_world
from top1_planner import quote_timing
from top1_planner.allocation_admission import _comparison
from top1_planner.available_crop_admission import quote_summary
from top1_planner.crop_input_value import crop_input_value
from top1_planner.recipe import compile_recipe
from top1_planner.redeployment import crop_cycles
from top1_planner.task_allocation import QuotePrices,_site_loss


def refinements(owner,obs,reference,*,price,day_states,first):
    region = reference.detail['region']
    points = tuple(r['point'] for r in eligible_points(owner,obs)
        if ('N' if r['point'][1]<5 else 'S')+('W' if r['point'][0]<5 else 'E')==region)[:8]
    if len(points)<=max(4,len(reference.detail['points'])):
        return ()
    output = []
    for offsets in (tuple(i%2 for i in range(len(points))),(1,)*len(points)):
        estimate = sum(crop_input_value(p,'CARROT',crop_cycles('CARROT',first+offset,p,period=3),
            price,False)-_site_loss(owner.programme,p,int(obs['step']),30,obs,price)
            for p,offset in zip(points,offsets))
        if estimate<=0:
            continue
        revision = build(owner,obs,points,first=first,offsets=offsets,period=3,day_states=day_states)
        revision.detail.update(estimate=estimate,region=region,reference=reference.programme.name,
            price_basis='Full-quote admitted regional reference, followed by complete phase-specific expansion and settlement')
        output.append(revision)
    return tuple(output)


def ordinary_response(obs,cfg,controller,*,free_seconds,shared_remaining,worlds):
    tick = int(obs['step'])
    if tick%72!=2 or not 362<=tick<=578:
        return herd_response(obs,cfg,controller,free_seconds=free_seconds,
            shared_remaining=shared_remaining,worlds=worlds)
    began = time.perf_counter()
    record = dict(kind='DEFERRED_CROP_ROUTE_RESPONSE',step=tick,parent=controller.programme.name,
        selected=controller.programme.name,changed=False,proposals=[],results=[],refinement_reference=None)
    def retain(reason):
        record.update(reason=reason,seconds=time.perf_counter()-began)
        return controller,record
    available = min(4.,max(0.,free_seconds)+max(0.,shared_remaining-2.))
    if available<2.5*quote_timing.estimate(tick,True)+.2:
        return retain('No budget for a fresh incumbent and complete crop response')
    deadline = began+available
    first = tick//24+1
    record.update(budget_seconds=available,free_seconds=free_seconds,shared_remaining=shared_remaining,
        construction_day=first)
    world,record['p110_intraday'] = intraday_world(obs,worlds(obs,future_model='uniform_mean')[1])
    def quote(plan):
        before = time.perf_counter()
        result = compile_recipe(obs,cfg,plan,world,settlement=True,
            continuation=controller.fork(programme=plan,settlement=True),
            bind_herd=controller.bind_herd,adaptive_crops=controller.adaptive_crops)
        quote_timing.observe(tick,True,time.perf_counter()-before)
        return result
    baseline = quote(controller.programme)
    summary = quote_summary(baseline.result,obs)
    price = QuotePrices(obs,baseline)
    small = variants(controller.programme,obs,controller,price=price,
        day_states=baseline.result['day_states'],first=first)
    # Compare more than one region before spending the remaining quotes on scale.
    initial,regions = [],set()
    for revision in small:
        if revision.detail['region'] not in regions:
            initial.append(revision)
            regions.add(revision.detail['region'])
            if len(initial)==2:
                break
    if len(initial)<2:
        initial.extend(r for r in small if r not in initial)
        initial = initial[:2]
    remaining = [r for r in small if r not in initial]
    winner = None
    evaluated = []
    def compare(options):
        nonlocal winner
        for revision in options:
            if len(record['results'])>=4 or time.perf_counter()+quote_timing.estimate(tick,True)*1.25+.15>deadline:
                break
            record['proposals'].append(dict(programme=revision.programme.name,points=revision.detail['points'],
                estimate=revision.detail['estimate'],mode=revision.detail['mode'],offsets=revision.detail.get('offsets')))
            compiled = quote(revision.programme)
            row = dict(_comparison(compiled,summary,obs,revision),programme=revision.programme.name,
                points=revision.detail['points'],mode=revision.detail['mode'],offsets=revision.detail.get('offsets'),
                minimum_cash=compiled.result['minimum_cash'],completed_before_deadline=time.perf_counter()<=deadline)
            if not row['completed_before_deadline']:
                row['rejection_reasons'].append('INCOMPLETE_WITHIN_BUDGET')
            if not math.isfinite(row['minimum_cash']) or row['minimum_cash']<0:
                row['rejection_reasons'].append('UNFUNDED_CROP_RESPONSE')
            row['lost_same_day_establishments'] = same_day_establishments(obs,baseline.result,compiled.result)
            if row['lost_same_day_establishments']:
                row['rejection_reasons'].append('UNDECLARED_CURRENT_ESTABLISHMENT_LOSS')
            row['admitted'] = not row['rejection_reasons']
            record['results'].append(row)
            evaluated.append((revision,row))
            if row['admitted'] and (winner is None or row['relative']>winner[1]['relative']):
                winner = revision,row
    compare(initial)
    if winner is not None:
        record['refinement_reference'] = winner[0].programme.name
        wider = refinements(controller,obs,winner[0],price=price,
            day_states=baseline.result['day_states'],first=first)
        compare(wider[:1])
        if wider and evaluated and evaluated[-1][0] is wider[0]:
            proposal,result = evaluated[-1]
            missing = {tuple(spec['point']) for spec in proposal.detail['lots']
                if result['harvested_by_lot'].get(spec['lot'],0)<=0}
            if 0<len(missing)<=2 and result['relative_delta']>0:
                points = tuple(proposal.detail['points'])
                offsets = tuple(1-offset if point in missing else offset
                    for point,offset in zip(points,proposal.detail['offsets']))
                repair = build(controller,obs,points,first=first,offsets=offsets,period=3,
                    day_states=baseline.result['day_states'])
                repair.detail.update(estimate=proposal.detail['estimate'],region=proposal.detail['region'],
                    reference=proposal.programme.name,phase_repair_points=tuple(sorted(missing)))
                compare((repair,))
            else:
                compare(wider[1:])
    compare(remaining)
    if winner is None:
        return retain('No complete funded inherited crop route beats the current continuation')
    revision,row = winner
    record.update(selected=revision.programme.name,changed=True,chosen=row,
        reason='A complete funded crop response improves the current continuation',seconds=time.perf_counter()-began)
    return controller.fork(programme=revision.programme,settlement=True),record
