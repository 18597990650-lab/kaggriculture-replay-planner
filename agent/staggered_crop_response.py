"""Stagger complete crop handoffs across retained routes without adding labor."""
from dataclasses import replace

from crop_route_response import build as build_site
from top1_planner.task_allocation import Revision


def build(controller,observation,points,*,first,offsets,period=3,day_states=()):
    points = tuple(tuple(p) for p in points)
    if len(points) != len(offsets) or len(set(points)) != len(points):
        raise ValueError('Every crop site requires exactly one phase')
    if any(type(n) is not int or n not in (0,1) for n in offsets):
        raise ValueError('Crop handoffs use adjacent unpaid days only')
    owner = controller
    components = []
    for point,offset in zip(points,offsets):
        revision = build_site(owner,observation,(point,),first=first+offset,period=period,day_states=day_states)
        components.append(revision)
        owner = owner.fork(programme=revision.programme,settlement=True)
    phases = '-'.join(f'{p[0]}.{p[1]}.{offset}' for p,offset in zip(points,offsets))
    name = controller.programme.name+f'-staggered-berry-{observation["step"]}-{first}-{period}-{phases}'
    programme = replace(owner.programme,name=name)
    assert programme.workers == controller.programme.workers
    if not controller.can_continue(programme):
        raise ValueError('Staggered crop handoffs changed an executed prefix')
    detail = dict(kind='productive_redeployment',mode='staggered_berry_turnover',
        step=int(observation['step']),item='CARROT',point=points[0],points=points,first=first,period=period,
        offsets=tuple(offsets),retired_animals=[],added_herds=[],marginal_wages=0)
    for key in ('lots','added_plantings','removed_tasks','work','inherited_visits'):
        detail[key] = [row for revision in components for row in revision.detail[key]]
    detail['boundary'] = 'Complete per-site crop cycles and warehouse returns on shared original routes. Phase changes defer actual construction rather than relabeling a paid crop; full native quote must cover displaced production and all funded work.'
    return Revision(programme,detail)
