"""Build one isolated Boey library while preserving the common verified engine."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
SOURCE = ROOT / 'research/supply35-regional-rotation-library-20260929/candidate'
COMPARATOR = ROOT / 'research/decem-dec01-rebuild-20260929/candidate'
SPECS = json.loads((HERE / 'CONFIG.json').read_bytes())['variants']


def digest(data):
    return hashlib.sha256(data).hexdigest()


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def inventory(root):
    return {p.relative_to(root).as_posix(): digest(p.read_bytes()) for p in sorted(root.rglob('*'))
            if p.is_file() and '__pycache__' not in p.parts}


def install_whole_search(target):
    path = target / 'search_policy.py'
    text = path.read_text(encoding='utf-8')
    changes = {
        '*,extra_variants=(),redeployment_seconds=0.0):':
            '*,extra_variants=(),redeployment_seconds=0.0,whole_programmes_only=False):',
        'if not any(managed(t) for actors in parent.days for sequence in actors.values() for t in sequence):':
            'if not whole_programmes_only and not any(managed(t) for actors in parent.days for sequence in actors.values() for t in sequence):',
        'variants=(*unpaid_herd_variants(parent,observation),*extra_variants,':
            'variants=() if whole_programmes_only else (*unpaid_herd_variants(parent,observation),*extra_variants,',
        '        core_end=min(deadline,max(elapsed(),seconds))':
            "        if whole_programmes_only:\n            detail=dict(changed=False,quotes=[],seconds=0.0,reason='whole_teacher_branch_before_paid_checkpoint')\n            expansions[label]=detail\n            return detail\n        core_end=min(deadline,max(elapsed(),seconds))",
        'allocation_reserve=min(seconds*.35,3*estimated_full*1.25+.15)':
            'allocation_reserve=0.0 if whole_programmes_only else min(seconds*.35,3*estimated_full*1.25+.15)',
    }
    for old, new in changes.items():
        assert text.count(old) == 1, old
        text = text.replace(old, new)
    path.write_text(text, encoding='utf-8')


def build(lineage, spec, all_routes, source_files):
    target = HERE / 'candidate'
    if target.exists():
        raise FileExistsError(target)
    routes = [r for r in all_routes if r['lineage'] == lineage]
    metadata = {r['name']: r for r in routes}
    plans = {name: json.loads((HERE / 'calendar_sources' / (name + '.json')).read_bytes()) for name in metadata}
    common = deepcopy(plans[spec['opening']])
    if spec.get('defer_first_day_sheep_feed'):
        point = spec['defer_first_day_sheep_feed']
        removed = 0
        for actor, tasks in common['days'][0].items():
            retained = [task for task in tasks if not (task['operation'] == 'FEED'
                and task['item'] == 'SHEEP' and task['point'] == point)]
            removed += len(tasks) - len(retained)
            common['days'][0][actor] = retained
        assert removed == 1
    common_paid = metadata[spec['opening']]['snapshots']['72']['layout']
    excluded_dirs = {'programmes', 'whole_opening_bank', 'whole_opening_yarn_bank', 'project_library'}
    excluded_files = {'LEARNED_POLICY.json', 'LEARNED_DOMAIN.json', 'learned_policy.py', 'learned_features.py',
        'whole_opening_family.py', 'whole_opening_yarn_continuations.py', 'SOURCE_ORIGIN.json',
        'VALIDATION_SUMMARY.json', 'STRATEGY_README_ZH.md', 'PERFORMANCE_zh.md', 'QUOTING_API_zh.md'}
    excluded = []
    for relative in source_files:
        parts = Path(relative).parts
        if parts[0] in excluded_dirs or relative in excluded_files or (len(parts) == 1 and ('MANIFEST' in relative or relative.startswith('COMPUTE_STAGE'))):
            excluded.append(relative)
            continue
        dest = target / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(SOURCE / relative, dest)
    library = dict(version=spec['version'], lineage=lineage, source_submissions=spec['submissions'],
        opening_anchor=spec['opening'], first_shop_anchors=spec['anchors'], paid_at_72=common_paid,
        groups={}, routes={}, opening_adjustment=spec.get('defer_first_day_sheep_feed'),
        contract='Only observed shops and exact paid identities admit whole continuations.')
    groups = {}
    for row in routes:
        assert row['submission'] in spec['submissions']
        assert row['snapshots']['72']['layout'] == common_paid, row['name']
        topology = json.dumps([r[:3] for r in row['snapshots']['144']['layout']], separators=(',', ':'))
        key = (row['group'], topology)
        groups.setdefault(key, []).append(row['name'])
    for (group_name, topology), names in groups.items():
        anchor_name = names[0]
        anchor = plans[anchor_name]
        target_paid = metadata[anchor_name]['snapshots']['144']['layout']
        library['groups'][anchor_name] = dict(anchor=anchor_name, group=group_name,
            first_shops=[shop for shop, value in spec['anchors'].items() if value == anchor_name],
            paid_at_144=target_paid, routes=names)
        for name in names:
            row = metadata[name]
            source_paid = row['snapshots']['144']['layout']
            aliases = {tuple(old): new[3] for old, new in zip(source_paid, target_paid)}
            plan = deepcopy(plans[name])
            plan['days'][:3] = deepcopy(common['days'][:3])
            plan['workers'][:3] = common['workers'][:3]
            plan['days'][3:6] = deepcopy(anchor['days'][3:6])
            plan['workers'][3:6] = anchor['workers'][3:6]
            remapped = 0
            for actors in plan['days'][6:]:
                for sequence in actors.values():
                    for task in sequence:
                        key = (*task['point'], task['item'], task['generation'])
                        if key in aliases and task['generation'] != aliases[key]:
                            task['generation'] = aliases[key]
                            remapped += 1
            dest = target / 'programmes' / (name + '.json')
            save(dest, plan)
            library['routes'][name] = {k: row[k] for k in ('episode', 'submission', 'seat', 'lineage',
                'group', 'first_family', 'first_shop', 'second_shop', 'second_family', 'manual_reason', 'replay_sha256')}
            library['routes'][name].update(sha256=digest(dest.read_bytes()),
                paid_group=anchor_name, normalized_generation_references=remapped)
    assert all(name in library['groups'] for name in spec['anchors'].values())
    save(target / 'programmes/LIBRARY.json', library)
    save(target / 'programmes/FAMILIES.json', {'boey02': list(library['routes'])})
    save(target / 'programmes/SECOND_WAVE_CONTEXTS.json', {})
    for name in ('parent_entry.py', 'whole_library_bridge.py', 'boey02_library.py'):
        shutil.copy2(HERE / name, target / name)
    path = target / 'native_policy.py'
    text = path.read_text(encoding='utf-8')
    replacements = {
        "glob('dsm-production-*.json')": "glob('boey02-*.json')",
        'if tick%72==0 and tick>=144 and day<=24 and _QUOTED_DAY!=day:':
            'if tick%72==0 and tick>=72 and day<=24 and _QUOTED_DAY!=day:',
        'if tick==144:': 'if tick in (72,144):',
    }
    for old, new in replacements.items():
        assert text.count(old) == 1, old
        text = text.replace(old, new)
    path.write_text(text, encoding='utf-8')
    path = target / 'top1_planner/planning_budget.py'
    text = path.read_text(encoding='utf-8')
    assert text.count('range(144,577,72)') == 1
    path.write_text(text.replace('range(144,577,72)', 'range(72,577,72)'), encoding='utf-8')
    install_whole_search(target)
    path = target / 'main.py'
    text = path.read_text(encoding='utf-8')
    path.write_text(text.replace('Complete physical admission with an isolated search scheduler.',
        spec['version'] + ': isolated Boey production schedules on the P110 physical engine.'), encoding='utf-8')
    save(target / 'SOURCE_ORIGIN.json', dict(version=spec['version'], source_submissions=spec['submissions'],
        teacher='Boey', own_calendars=len(routes), old_own_calendars_retained=0,
        engine_parent=str(SOURCE), predictor='P110 q7 plus supply35 prior', own_route_model=False,
        boundaries='Successful teacher work intentions; runtime movement, procurement, sales and adaptation use the retained engine.'))
    save(HERE / 'MANIFEST.json', dict(version=spec['version'], files=inventory(target),
        formal_source_files=source_files, formal_source_unchanged=True, excluded_files=excluded))
    return dict(version=spec['version'], programmes=len(routes), groups=len(groups), files=len(inventory(target)),
        main_sha256=digest((target / 'main.py').read_bytes()))


def main():
    routes = json.loads((HERE / 'SELECTION.json').read_bytes())['routes']
    source_files = inventory(SOURCE)
    comparator_files = inventory(COMPARATOR)
    result = [build(lineage, spec, routes, source_files) for lineage, spec in SPECS.items()]
    assert inventory(SOURCE) == source_files
    assert inventory(COMPARATOR) == comparator_files
    save(HERE / 'COMPARISON_MANIFEST.json', dict(path=str(COMPARATOR), files=comparator_files))
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
