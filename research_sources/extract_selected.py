"""Reconcile selected complete teacher routes through the official interpreter."""
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
ENGINE = ROOT / 'research/supply35-regional-rotation-library-20260929/candidate'
sys.path[:0] = [str(ENGINE), str(ROOT / 'research/top10-missing-libraries-20260930/evaluation_dependencies' / 'g7p01/tools')]
from run_local import Attr, engine
from shop_schedule import RecordedShopSchedule
from top1_planner.programmes import ProductionProgramme


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def extract(job):
    spec, row = job
    path = Path(row['path'])
    raw = path.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == row['sha256']
    replay = json.loads(raw)
    seat = row['seat']
    cfg = Attr(deepcopy(replay['configuration']))
    cfg.pop('seed', None)
    env = SimpleNamespace(configuration=cfg, info=deepcopy(replay['info']), done=False)
    rules = engine()
    shops = RecordedShopSchedule(replay, source=str(path), source_sha256=row['sha256'])
    shops.attach(rules, 24)
    states = [Attr(action={'farmer': ['PASS'], 'hands': [], 'market': []}, reward=None,
        status='ACTIVE', observation=Attr(step=0, player=side)) for side in (0, 1)]
    rules._initialize(states, env)
    days, workers, events = [{} for _ in range(30)], [0] * 30, []
    skipped = Counter()
    tick = 0
    apply = rules._apply_unit_action

    def track(farm, private, actor, action, *args, **kwargs):
        if farm is not states[0].observation.farms[seat]:
            return apply(farm, private, actor, action, *args, **kwargs)
        day = tick // 24
        workers[day] = max(workers[day], len(farm['hands']))
        point = tuple(rules._farmer_position(farm, actor))
        tile = deepcopy(farm['tiles'][point[1]][point[0]])
        before = (tile, deepcopy(private['inventories'][actor]), dict(private['shed']), dict(private['seeds']))
        result = apply(farm, private, actor, action, *args, **kwargs)
        after = (farm['tiles'][point[1]][point[0]], private['inventories'][actor], private['shed'], private['seeds'])
        op = action[0]
        if op in rules.FARMER_MOVES or op == 'PASS':
            return result
        if before == after:
            skipped[op] += 1
            return result
        item = action[1] if op in ('PLACE', 'PLANT', 'PICKUP') else (
            tile.get('animal') or tile.get('crop') if isinstance(tile, dict) else None)
        deposit = op == 'PLACE' and private['shed'].get(item, 0) > before[2].get(item, 0)
        generation = day if op == 'PLANT' or (op == 'PLACE' and item in rules.ANIMALS) else (
            tile.get('placed_day') if tile.get('animal') else tile.get('planted_day')) if isinstance(tile, dict) else None
        if deposit:
            op, generation = 'DEPOSIT', None
        elif op == 'DIG' and item in rules.CROPS:
            op = 'CLEAR_CROP'
        task = dict(point=point, operation=op, item=item, generation=generation)
        days[day].setdefault(str(actor), []).append(task)
        events.append(dict(tick=tick, actor=actor, **task))
        return result

    rules._apply_unit_action = track
    for tick in range(719):
        for side in (0, 1):
            states[side].observation.step = tick
            states[side].action = deepcopy(replay['steps'][tick+1][side]['action'])
        states = rules.interpreter(states, env)
        for side in (0, 1):
            states[side].observation.step = tick+1
            expected = dict(replay['steps'][tick+1][0]['observation'], **replay['steps'][tick+1][side]['observation'])
            for key in ('farms', 'private', 'market', 'town', 'day', 'hour', 'player'):
                assert states[side].observation[key] == expected[key], (row['episode'], tick+1, side, key)
        shops.verify_frame(states, tick+1)
    assert [state.reward for state in states] == replay['rewards']
    programme = dict(name=spec['name'], days=days, workers=workers)
    ProductionProgramme.from_dict(programme)
    save(HERE / 'calendar_sources' / (spec['name'] + '.json'), programme)
    save(HERE / 'calendar_sources' / (spec['name'] + '-events.json'), events)
    origin = dict(spec, submission=row['submission'], seat=seat, replay=row['path'],
        replay_sha256=row['sha256'], native_transitions_verified=719, skipped_no_effect=dict(skipped),
        tasks=len(events), worker_days=sum(workers), snapshots=row['snapshots'])
    save(HERE / 'calendar_sources' / (spec['name'] + '-origin.json'), origin)
    return origin


def main():
    target = HERE / 'SELECTION.json'
    if target.exists():
        raise FileExistsError(target)
    rows = json.loads((HERE / 'REPLAY_INDEX.json').read_bytes())['rows']
    indexed = {row['episode']: row for row in rows}
    specs = json.loads((HERE / 'SELECTION_PENDING.json').read_bytes())['routes']
    results = []
    with ProcessPoolExecutor(max_workers=4) as pool:
        for future in as_completed([pool.submit(extract, (spec, indexed[spec['episode']])) for spec in specs]):
            row = future.result()
            results.append(row)
            print(json.dumps({key: row[key] for key in ('name', 'episode', 'submission', 'tasks', 'worker_days')}), flush=True)
    assert len(results) == len(specs) and {row['submission'] for row in results} == {56686734, 56686742}
    save(target, dict(routes=sorted(results, key=lambda row: row['name']), old_calendars_retained=0,
        native_transitions_verified=719*len(results), source_submissions=[56686734, 56686742],
        boundary='Successful work intentions, not market-action playback. Each complete source is '
                 'physically reconciled; canonical prefix normalization and live execution remain to validate.'))


if __name__ == '__main__':
    main()
