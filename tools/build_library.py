"""Normalize curated whole programmes into compatible paid-prefix families."""
import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path

from common import ROOT, load_json, write_json


def build(config_path, routes_path, raw_directory, output):
    if output.exists():
        raise FileExistsError(output)
    config = load_json(config_path)
    routes = load_json(routes_path)['routes']
    if len(config['variants']) != 1:
        raise ValueError('Build each isolated lineage in its own output directory')
    lineage, spec = next(iter(config['variants'].items()))
    routes = [r for r in routes if r['lineage'] == lineage]
    metadata = {r['name']: r for r in routes}
    if not metadata or len(metadata) != len(routes):
        raise ValueError('Empty library or duplicate programme name')
    plans = {name: load_json(raw_directory / (name + '.json')) for name in metadata}
    common = deepcopy(plans[spec['opening']])
    if spec.get('defer_first_day_sheep_feed'):
        removed = 0
        for actor, tasks in common['days'][0].items():
            retained = [task for task in tasks if not (
                task['operation'] == 'FEED' and task['item'] == 'SHEEP'
                and task['point'] == spec['defer_first_day_sheep_feed'])]
            removed += len(tasks) - len(retained)
            common['days'][0][actor] = retained
        if removed != 1:
            raise ValueError('The declared opening adjustment must identify one task')
    common_paid = metadata[spec['opening']]['snapshots']['72']['layout']
    library = dict(version=spec['version'], lineage=lineage, source_submissions=spec['submissions'],
        opening_anchor=spec['opening'], first_shop_anchors=spec['anchors'], paid_at_72=common_paid,
        groups={}, routes={}, opening_adjustment=spec.get('defer_first_day_sheep_feed'),
        contract='Only observed shops and exact paid identities admit whole continuations.')
    groups = {}
    for row in routes:
        if row['submission'] not in spec['submissions']:
            raise ValueError('Unexpected source submission')
        if row['snapshots']['72']['layout'] != common_paid:
            raise ValueError('Incompatible opening: ' + row['name'])
        topology = json.dumps([r[:3] for r in row['snapshots']['144']['layout']], separators=(',', ':'))
        groups.setdefault((row['group'], topology), []).append(row['name'])
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
            if [r[:3] for r in source_paid] != [r[:3] for r in target_paid]:
                raise ValueError('Incompatible paid topology')
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
            dest = output / (name + '.json')
            write_json(dest, plan)
            library['routes'][name] = {k: row[k] for k in ('episode', 'submission', 'seat', 'lineage',
                'group', 'first_family', 'first_shop', 'second_shop', 'second_family', 'manual_reason', 'replay_sha256')}
            library['routes'][name].update(sha256=hashlib.sha256(dest.read_bytes()).hexdigest(),
                paid_group=anchor_name, normalized_generation_references=remapped)
    if any(name not in library['groups'] for name in spec['anchors'].values()):
        raise ValueError('Missing shop-family anchor')
    write_json(output / 'LIBRARY.json', library)
    write_json(output / 'FAMILIES.json', {lineage: list(library['routes'])})
    write_json(output / 'SECOND_WAVE_CONTEXTS.json', {})
    return dict(programmes=len(routes), groups=len(groups), output=str(output))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=ROOT / 'data/library_config.json')
    parser.add_argument('--routes', type=Path, default=ROOT / 'data/source_routes.json')
    parser.add_argument('--raw', type=Path, default=ROOT / 'data/raw_programmes')
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(build(args.config, args.routes, args.raw, args.out)))


if __name__ == '__main__':
    main()

