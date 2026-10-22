"""Offer complete small and phased regional replacements under one quote cap."""
from crop_route_response import eligible_points,variants as small_variants
from staggered_crop_response import build
from top1_planner.crop_input_value import crop_input_value
from top1_planner.redeployment import crop_cycles
from top1_planner.task_allocation import _site_loss


def variants(programme,observation,controller,*,price,day_states=(),first=None):
    small = small_variants(programme,observation,controller,price=price,day_states=day_states,first=first)
    if not small:
        return ()
    tick = int(observation['step'])
    first = tick//24 if first is None else first
    owner = controller.fork(programme=programme,settlement=True)
    eligible = eligible_points(owner,observation)
    region = small[0].detail['region']
    points = tuple(r['point'] for r in eligible if ('N' if r['point'][1]<5 else 'S')
        +('W' if r['point'][0]<5 else 'E') == region)[:8]
    if len(points) <= 4:
        return small
    phases = []
    for offsets in (tuple(i%2 for i in range(len(points))),(1,)*len(points)):
        estimate = sum(crop_input_value(p,'CARROT',crop_cycles('CARROT',first+offset,p,period=3),
            price,False)-_site_loss(programme,p,tick,30,observation,price)
            for p,offset in zip(points,offsets))
        if estimate <= 0:
            continue
        revision = build(owner,observation,points,first=first,offsets=offsets,period=3,day_states=day_states)
        revision.detail.update(estimate=estimate,region=region,
            price_basis='Phase-specific biological cash after displaced crops and seeds; full quotation decides physical completion, funding and bilateral settlement')
        phases.append(revision)
    phases.sort(key=lambda r:(-r.detail['estimate'],r.programme.name))
    # Keep the highest-ranked small response before testing wider plot coverage.
    # Neither a large gross harvest nor a partial quotation can admit a plan.
    choices = [small[0]]
    if phases:
        choices.append(phases[0])
    if len(small)>1:
        choices.append(small[1])
    if len(phases)>1:
        choices.append(phases[1])
    for revision in small[2:]:
        if len(choices)>=4:
            break
        choices.append(revision)
    return tuple(choices[:4])
