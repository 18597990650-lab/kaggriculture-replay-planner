"""Validate the manually specified production coverage before any live evaluation."""
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent


def family(shop):
    return {'ICE_CREAM_SHOP': 'milk', 'SMOOTHIE_SHOP': 'milk',
        'FARMERS_MARKET': 'fruit', 'BRUNCH_SPOT': 'fruit', 'PET_CAFE': 'pet',
        'BAKERY': 'bakery', 'PIZZA_SHOP': 'pizza', 'YARN_STORE': 'wool'}[shop]


def main():
    config = json.loads((HERE / 'CONFIG.json').read_bytes())
    rows = json.loads((HERE / 'REPLAY_INDEX.json').read_bytes())['rows']
    indexed = {row['episode']: row for row in rows}
    assert len(indexed) == len(rows), 'Duplicate episode seats need explicit handling'
    results, groups, prefixes = [], {}, {}
    for index, spec in enumerate(config['routes']):
        row = indexed[spec['episode']]
        lineage = spec['lineage']
        variant = config['variants'][lineage]
        expected = variant['submissions'] if 'submissions' in variant else [variant['submission']]
        assert row['submission'] in expected
        prefixes.setdefault(lineage, row['snapshots']['72']['layout_hash'])
        assert prefixes[lineage] == row['snapshots']['72']['layout_hash']
        topology = [r[:3] for r in row['snapshots']['144']['layout']]
        key = lineage, spec['group']
        groups.setdefault(key, topology)
        assert groups[key] == topology
        first, second = row['snapshots']['144']['shops']
        results.append(dict(spec, name=f"{config['prefix']}-{index:02d}",
            first_family=family(first), first_shop=first, second_family=family(second), second_shop=second))
    target = HERE / 'SELECTION_PENDING.json'
    if target.exists():
        raise FileExistsError(target)
    target.write_text(json.dumps(dict(routes=results, selection=config['selection_reason']),
        ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(dict(routes=len(results), groups=len(groups), prefixes=prefixes)))


if __name__ == '__main__':
    main()
