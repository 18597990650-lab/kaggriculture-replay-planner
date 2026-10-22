"""Extract successful production tasks, preserving seat and recorded shop stream."""
import argparse
from collections import Counter
from copy import deepcopy
import json
from pathlib import Path

from common import initial_state, load_engine, read_replay, verify_frame, write_json
from shop_schedule import RecordedShopSchedule


def extract(replay_path, seat, name):
    replay, sha256 = read_replay(replay_path)
    rules = load_engine()
    shops = RecordedShopSchedule(replay, source=replay_path, source_sha256=sha256)
    shops.attach(rules, 24)
    states, env = initial_state(replay, rules)
    verify_frame(states, replay, 0)
    days, workers, events = [{} for _ in range(30)], [0] * 30, []
    skipped = Counter()
    tick = 0
    original = rules._apply_unit_action

    def track(farm, private, actor, action, *args, **kwargs):
        if farm is not states[0].observation.farms[seat]:
            return original(farm, private, actor, action, *args, **kwargs)
        day = tick // 24
        workers[day] = max(workers[day], len(farm['hands']))
        point = tuple(rules._farmer_position(farm, actor))
        tile = deepcopy(farm['tiles'][point[1]][point[0]])
        before = (tile, deepcopy(private['inventories'][actor]),
                  dict(private['shed']), dict(private['seeds']))
        result = original(farm, private, actor, action, *args, **kwargs)
        after = (farm['tiles'][point[1]][point[0]], private['inventories'][actor],
                 private['shed'], private['seeds'])
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
            tile.get('placed_day') if tile.get('animal') else tile.get('planted_day')
        ) if isinstance(tile, dict) else None
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
            states[side].action = deepcopy(replay['steps'][tick + 1][side]['action'])
        states = rules.interpreter(states, env)
        for side in (0, 1):
            states[side].observation.step = tick + 1
        verify_frame(states, replay, tick + 1)
        shops.verify_frame(states, tick + 1)
    if [state.reward for state in states] != replay['rewards']:
        raise AssertionError('Final rewards differ from the recording')
    return dict(name=name, days=days, workers=workers), events, dict(
        episode=replay['info']['EpisodeId'], seat=seat, replay_sha256=sha256,
        native_transitions_verified=719, tasks=len(events), worker_days=sum(workers),
        skipped_no_effect=dict(skipped), fixed_shops=True,
        source_file=Path(replay_path).name,
        scope='Verified work intentions, not literal movement or market-order playback')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--replay', type=Path, required=True)
    parser.add_argument('--seat', type=int, choices=(0, 1), required=True)
    parser.add_argument('--name', required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    if args.out.exists():
        raise FileExistsError(args.out)
    plan, events, audit = extract(args.replay, args.seat, args.name)
    write_json(args.out, plan)
    write_json(args.out.with_name(args.out.stem + '-events.json'), events)
    write_json(args.out.with_name(args.out.stem + '-audit.json'), audit)
    print(json.dumps(audit, ensure_ascii=False))


if __name__ == '__main__':
    main()

