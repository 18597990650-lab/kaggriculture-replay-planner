"""Whole Boey schedules selected before payment, with paid-state continuation gates."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path


def shop_family(shop):
    return {'ICE_CREAM_SHOP': 'milk', 'SMOOTHIE_SHOP': 'milk',
        'FARMERS_MARKET': 'fruit', 'BRUNCH_SPOT': 'fruit', 'PET_CAFE': 'pet',
        'BAKERY': 'bakery', 'PIZZA_SHOP': 'pizza', 'YARN_STORE': 'wool'}[shop]


def identities(observation):
    farm = observation['farms'][int(observation['player'])]
    result = []
    for y, line in enumerate(farm['tiles']):
        for x, tile in enumerate(line):
            if not isinstance(tile, dict):
                continue
            if tile.get('animal'):
                result.append([x, y, tile['animal'], tile['placed_day']])
            elif tile.get('crop'):
                result.append([x, y, tile['crop'], tile['planted_day']])
    return result


def install(runtime):
    from top1_planner import macro_projects
    from top1_planner import recipe

    macro_projects._regional = lambda *args, **kwargs: None
    unplanned_harvest = recipe.take_orphan_harvest

    def protect_opening(controller, actor, farm, private, market, tick, points):
        # The paid checkpoint requires the complete planned opening crop layout.
        if tick < 72:
            return None
        return unplanned_harvest(controller, actor, farm, private, market, tick, points)

    recipe.take_orphan_harvest = protect_opening
    root = Path(__file__).resolve().parent
    library = json.loads((root / 'programmes/LIBRARY.json').read_bytes())
    indexed = {plan.name: plan for plan in runtime._BASES}
    if not indexed or set(indexed) != set(library['routes']):
        raise ValueError('Boey runtime must load exactly the declared own-policy library')
    for name, metadata in library['routes'].items():
        if metadata['submission'] not in library['source_submissions']:
            raise ValueError('Mixed own-policy teachers are forbidden')
        actual = hashlib.sha256((root / 'programmes' / (name + '.json')).read_bytes()).hexdigest()
        if actual != metadata['sha256']:
            raise ValueError('Programme hash mismatch: ' + name)
    original_factory = runtime.RecipeCompiler
    original_agent = runtime.agent
    original_choose = runtime.choose_programme
    state = dict(step=None)

    def opening(observation, configuration, controller, seconds):
        # The complete teacher opening owns its labour and material obligations.
        return controller, None

    def family_bases(observation, unused_bases, unused_families):
        tick = int(observation['step'])
        controller = runtime._CONTROLLER
        current = controller.programme
        actual = identities(observation)
        shops = observation['town']['unlocked_shops']
        detail = dict(kind='BOEY02_LIBRARY_DECISION', step=tick, version=library['version'],
            lineage=library['lineage'], previous=current.name, visible_shops=list(shops),
            adopted=False, missing=[], extra=[])
        if tick == 72:
            if len(shops) != 1:
                raise ValueError('First-shop decision needs exactly one public shop')
            expected = library['paid_at_72']
            detail['missing'] = [r for r in expected if r not in actual]
            detail['extra'] = [r for r in actual if r not in expected]
            family = shop_family(shops[0])
            anchor = indexed[library['first_shop_anchors'][shops[0]]]
            group_names = [group['anchor'] for group in library['groups'].values()
                           if shops[0] in group['first_shops']]
            group_names.sort(key=lambda name: (name != anchor.name,
                library['routes'][name]['first_shop'] != shops[0], name))
            candidates = []
            if not detail['missing'] and not detail['extra']:
                for name in group_names:
                    donor = indexed[name]
                    plan = replace(donor, days=current.days[:3] + donor.days[3:],
                                   workers=current.workers[:3] + donor.workers[3:])
                    if controller.can_continue(plan):
                        candidates.append(plan)
            if not candidates:
                candidates = [current]
                detail['reason'] = 'keep_actual_paid_plan'
            else:
                detail.update(adopted=True, reason='public_first_shop_complete_unpaid_branches')
            detail['first_family'] = family
        elif tick == 144:
            if len(shops) != 2:
                raise ValueError('Second-shop decision needs two public shops')
            family = shop_family(shops[0])
            second = shop_family(shops[1])
            candidates = [current]
            compatible_groups = [group for group in library['groups'].values()
                                 if group['paid_at_144'] == actual]
            names = [name for group in compatible_groups for name in group['routes']]
            names.sort(key=lambda name: (
                library['routes'][name]['second_shop'] != shops[1],
                library['routes'][name]['second_family'] != second,
                library['routes'][name]['first_shop'] != shops[0], name))
            for name in names:
                donor = indexed[name]
                plan = replace(donor, days=current.days[:6] + donor.days[6:],
                               workers=current.workers[:6] + donor.workers[6:])
                if plan != current and controller.can_continue(plan):
                    candidates.append(plan)
            detail.update(adopted=len(candidates) > 1,
                reason='exact_paid_layout_full_continuations' if len(candidates) > 1 else 'keep_actual_paid_plan',
                first_family=family, second_family=second,
                matched_paid_groups=[g['anchor'] for g in compatible_groups])
        else:
            raise ValueError('Unexpected whole-library decision step')
        detail['candidates'] = [plan.name for plan in candidates]
        runtime._TRACE.append(detail)
        return 'boey02_' + library['lineage'] + '_' + family, candidates

    def factory(programme, configuration, **kwargs):
        owner = runtime._CONTROLLER
        if (state['step'] in (72, 144) and owner is not None
                and 'adaptive_crops' not in kwargs and owner.can_continue(programme)):
            return owner.fork(programme=programme, settlement=kwargs.get('settlement', owner.settlement))
        return original_factory(programme, configuration, **kwargs)

    def agent(observation, configuration):
        tick = int(observation['step']) if 'step' in observation else int(observation['day'])*24+int(observation['hour'])
        state['step'] = tick
        action = original_agent(observation, configuration)
        if tick == 0:
            runtime._TRACE.append(dict(kind='BOEY02_LIBRARY_IDENTITY', step=0, version=library['version'],
                calendars=list(indexed), teacher_submissions=library['source_submissions'],
                old_own_calendars=0, own_route_model=False, predictor='P110 retained'))
        return action

    def choose(observation, *args, **kwargs):
        kwargs['whole_programmes_only'] = int(observation['step']) == 72
        return original_choose(observation, *args, **kwargs)

    default = library['opening_anchor']
    runtime._BASES = [indexed[default], *(p for name, p in indexed.items() if name != default)]
    runtime.optimize_opening_work = opening
    runtime.family_bases = family_bases
    runtime.RecipeCompiler = factory
    runtime.second_wave_variants = lambda *args, **kwargs: ()
    runtime.choose_programme = choose
    runtime.agent = agent
