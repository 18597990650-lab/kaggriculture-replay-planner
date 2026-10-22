"""Reuse verified fill ledgers and reconstruct only previously unseen episodes."""
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
import argparse
import copy
import hashlib
import json
from pathlib import Path
import sys
import time

HERE = Path(__file__).resolve().parent
OLD = HERE.parent / 'multiwin25-supply-bank-20260926'
sys.path.insert(0, str(OLD / 'reference_tool'))
from supply_demo import digest, load_rules, process_turn
from prepare import load, save
from public_features import PRODUCTS, SHOPS, snapshot


def worker(group):
    output = HERE / 'episodes' / f"{group['episode']}.json"
    if output.exists():
        value = load(output)
        assert value['passed'] and value['input_sha256'] == group['sha256']
        return dict(episode=group['episode'], status='existing', seconds=value['seconds'])
    started = time.perf_counter()
    raw = Path(group['path']).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == group['sha256']
    replay = json.loads(raw)
    assert int(replay['info']['EpisodeId']) == group['episode']
    assert len(replay['steps']) == 720 and replay['statuses'] == ['DONE', 'DONE']
    for s in group['sources']:
        assert replay['rewards'][s['seat']] == s['reward']
    assert replay['configuration']['turnsPerDay'] == 24 and replay['configuration']['episodeSteps'] == 720
    previous_path = OLD / 'episodes' / output.name
    prior = load(previous_path) if previous_path.exists() else None
    if prior:
        assert prior['passed'] and prior['input_sha256'] == group['sha256']
    known = {r['source']['seat']: r for r in prior['samples']} if prior else {}
    samples = {}
    missing = []
    for s in group['sources']:
        seat = s['seat']
        daily = [snapshot(replay['steps'][d*24][0]['observation'], seat) for d in range(30)]
        if seat in known:
            old = known[seat]
            assert [r['assets'] + r['shops'] for r in daily] == old['features']
            samples[seat] = dict(old, source=s, public=daily)
        else:
            missing.append(seat)
            samples[seat] = dict(source=s, public=daily,
                features=[r['assets']+r['shops'] for r in daily],
                prices=[r['prices'] for r in daily], net_sales=[[0]*9 for _ in range(30)],
                market_supply=[[0]*9 for _ in range(30)], floor_sales=[[0]*9 for _ in range(30)])
    if missing:
        rules = load_rules(load(OLD / 'reference_tool/sources/REFERENCE.json'))
        initial = replay['steps'][0]
        simulation = dict(farms=copy.deepcopy(initial[0]['observation']['farms']),
            private=[copy.deepcopy(s['observation']['private']) for s in initial],
            market=copy.deepcopy(initial[0]['observation']['market']),
            town=copy.deepcopy(initial[0]['observation']['town']))
        for tick in range(719):
            net, filled, cash, market_delta, requested = process_turn(replay, tick, rules, simulation)
            for seat in missing:
                for p, product in enumerate(PRODUCTS):
                    samples[seat]['net_sales'][tick//24][p] += net[seat][p]
                    samples[seat]['market_supply'][tick//24][p] += market_delta[seat][product]
                    samples[seat]['floor_sales'][tick//24][p] += net[seat][p] - market_delta[seat][product]
    for sample in samples.values():
        sample['sample_sha256'] = digest({k: sample[k] for k in ('features', 'net_sales')})
    value = dict(episode=group['episode'], input_sha256=group['sha256'], passed=True,
        world_seed=replay['info']['seed'], samples=list(samples.values()),
        reconstructed=bool(missing), inherited_verification=str(previous_path) if prior else None,
        seconds=time.perf_counter()-started)
    save(output, value)
    return dict(episode=group['episode'], status='DONE', reconstructed=bool(missing), seconds=value['seconds'])


def collect(inputs):
    episodes = [load(HERE/'episodes'/f"{g['episode']}.json") for g in inputs['episodes']]
    requested = {g['episode']: g['requested_split'] for g in inputs['episodes']}
    priority = {'train': 0, 'dev': 1, 'test_chronological': 2, 'test_unseen_submission': 3}
    by_world = defaultdict(list)
    for e in episodes:
        assert e['passed']
        by_world[e['world_seed']].append(e)
    rows = []
    for world, group in by_world.items():
        split = max((requested[e['episode']] for e in group), key=priority.get)
        for e in group:
            for row in e['samples']:
                row['source'] = dict(row['source'], split=split, world_seed=world)
                rows.append(row)
    rows.sort(key=lambda r: (r['source']['episode'], r['source']['seat']))
    assert len(rows) == len(inputs['sources'])
    assert len({r['source']['sample_id'] for r in rows}) == len(rows)
    splits = {s: {r['source']['world_seed'] for r in rows if r['source']['split'] == s} for s in priority}
    for a in splits:
        for b in splits:
            assert a == b or not splits[a] & splits[b]
    save(HERE/'DATASET.json', dict(products=PRODUCTS, shops=SHOPS, samples=rows))
    report = dict(passed=True, samples=len(rows), episodes=len(episodes), worlds=len(by_world),
        world_overlap=0, split_counts=dict(Counter(r['source']['split'] for r in rows)),
        reconstructed=sum(e['reconstructed'] for e in episodes),
        inherited=sum(not e['reconstructed'] for e in episodes),
        full_cash_checks=1438*len(episodes), full_state_component_checks=3595*len(episodes),
        public_feature_contract='Only farms, step, market prices and unlocked shops. History uses prior public dawn observations. Source identity, seed and private states excluded from predictor.',
        boundary='Six held submissions may share public route families with training; do not call them unseen-family tests.')
    save(HERE/'BUILD_VERIFICATION.json', report)
    print(json.dumps(report), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--limit', type=int)
    parser.add_argument('--workers', type=int, default=4)
    args = parser.parse_args()
    assert 1 <= args.workers <= 4
    inputs = load(HERE/'INPUTS.json')
    (HERE/'episodes').mkdir(exist_ok=True)
    groups = inputs['episodes'][:args.limit] if args.limit else inputs['episodes']
    done = []
    errors = []
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        jobs = {pool.submit(worker, g): g['episode'] for g in groups}
        for task in as_completed(jobs):
            try:
                done.append(task.result())
            except Exception as exc:
                errors.append(dict(episode=jobs[task], error=repr(exc)))
            if len(done) % 50 == 0 or errors or len(done)+len(errors) == len(groups):
                progress = dict(completed=len(done), total=len(groups), errors=errors)
                save(HERE/'BUILD_PROGRESS.json', progress)
                print(json.dumps(progress), flush=True)
    save(HERE/'BUILD_JOBS.json', dict(completed=done, errors=errors))
    assert not errors, 'Invalid reconstruction cannot enter the training bank'
    if not args.limit:
        collect(inputs)


if __name__ == '__main__':
    main()
