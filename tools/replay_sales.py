"""Generate public features and successful trade labels from an exact replay."""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys

from common import ROOT, initial_state, load_engine, observation, read_replay, verify_frame, write_json
from shop_schedule import RecordedShopSchedule


def extract_labels(path, seat):
    sys.path.insert(0, str(ROOT / 'agent'))
    from public_features import PRODUCTS, snapshot
    from phase_features import extra_features
    replay, sha256 = read_replay(path)
    rules = load_engine()
    shops = RecordedShopSchedule(replay, source=path, source_sha256=sha256)
    shops.attach(rules, 24)
    states, env = initial_state(replay, rules)
    verify_frame(states, replay, 0)
    daily = []
    for day in range(30):
        obs = observation(replay, day * 24, 1 - seat)
        row = snapshot(obs, seat)
        row.update(extra_features(obs, seat))
        daily.append(row)
    net = [[0] * 9 for _ in range(30)]
    slots = [[[0] * 9 for _ in range(6)] for _ in range(30)]
    receipts = []
    commit = rules._commit_unit
    tick = 0

    def track(op, item, price, farm, private, market, *args, **kwargs):
        accepted = commit(op, item, price, farm, private, market, *args, **kwargs)
        if accepted and farm is states[0].observation.farms[seat] and op in ('SELL', 'BUY_PRODUCT'):
            index = PRODUCTS.index(item)
            direction = 1 if op == 'SELL' else -1
            net[tick // 24][index] += direction
            slots[tick // 24][tick % 24 // 4][index] += direction
            receipts.append(dict(step=tick, operation=op, product=item, price=price))
        return accepted

    rules._commit_unit = track
    for tick in range(719):
        for side in (0, 1):
            states[side].observation.step = tick
            states[side].action = deepcopy(replay['steps'][tick + 1][side]['action'])
        states = rules.interpreter(states, env)
        for side in (0, 1):
            states[side].observation.step = tick + 1
        verify_frame(states, replay, tick + 1)
        shops.verify_frame(states, tick + 1)
    if [s.reward for s in states] != replay['rewards']:
        raise AssertionError('Final reward mismatch')
    for day in range(30):
        if [sum(row[p] for row in slots[day]) for p in range(9)] != net[day]:
            raise AssertionError('Four-hour and daily labels disagree')
    return dict(episode=replay['info']['EpisodeId'], seat=seat, replay_sha256=sha256,
        world_seed=replay['info']['seed'], products=PRODUCTS, public=daily,
        net_sales=net, net_sales_4h=slots, actual_fills=receipts,
        verified_transitions=719,
        contract='Public inputs; successful SELL minus BUY_PRODUCT labels. '
                 'Source identity and world_seed are split/provenance metadata, never model features. '
                 'Full observations and actions are used only by the offline label interpreter.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--replay', type=Path, required=True)
    parser.add_argument('--seat', type=int, choices=(0, 1), required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    if args.out.exists():
        raise FileExistsError(args.out)
    result = extract_labels(args.replay, args.seat)
    write_json(args.out, result)
    print(json.dumps(dict(episode=result['episode'], seat=args.seat,
                         verified_transitions=719, filled_units=len(result['actual_fills']))))


if __name__ == '__main__':
    main()

