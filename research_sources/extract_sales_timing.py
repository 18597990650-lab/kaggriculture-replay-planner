"""Reconstruct four-hour actual fills using the official continuous interpreter."""
from concurrent.futures import ProcessPoolExecutor, as_completed
import copy
import hashlib
import json
from pathlib import Path
import sys
import time

HERE = Path(__file__).resolve().parent
PARENT = HERE.parent
REFERENCE = PARENT.parent/'multiwin25-supply-bank-20260926/reference_tool'
sys.path.insert(0, str(REFERENCE))
sys.path.insert(1, str(PARENT))
from supply_demo import load_rules, process_turn
from prepare import load, save

RULES = None


def initialize():
    global RULES
    RULES = load_rules(load(REFERENCE/'sources/REFERENCE.json'))


def worker(group):
    output = HERE/'episodes'/f"{group['episode']}.json"
    if output.exists():
        record = load(output)
        assert record['passed'] and record['input_sha256'] == group['sha256']
        return
    raw = Path(group['path']).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == group['sha256']
    replay = json.loads(raw)
    assert int(replay['info']['EpisodeId']) == group['episode']
    assert len(replay['steps']) == 720 and replay['statuses'] == ['DONE','DONE']
    expected = load(PARENT/'episodes'/output.name)
    assert expected['passed'] and expected['input_sha256'] == group['sha256']
    days = {r['source']['seat']: r['net_sales'] for r in expected['samples']}
    empty = lambda: [[[0]*9 for _ in range(6)] for _ in range(30)]
    tables = {s['seat']: dict(net=empty(), sell=empty(), buy=empty(), market=empty()) for s in group['sources']}
    first = replay['steps'][0]
    simulation = dict(farms=copy.deepcopy(first[0]['observation']['farms']),
        private=[copy.deepcopy(f['observation']['private']) for f in first],
        market=copy.deepcopy(first[0]['observation']['market']),
        town=copy.deepcopy(first[0]['observation']['town']))
    for tick in range(719):
        net, filled, cash, delta, requested = process_turn(replay, tick, RULES, simulation)
        day, slot = tick//24, tick%24//4
        for seat, table in tables.items():
            for p, product in enumerate(RULES.PRODUCTS):
                table['net'][day][slot][p] += net[seat][p]
                table['sell'][day][slot][p] += filled[seat][('SELL',product)]
                table['buy'][day][slot][p] += filled[seat][('BUY_PRODUCT',product)]
                table['market'][day][slot][p] += delta[seat][product]
    samples = []
    for source in group['sources']:
        table = tables[source['seat']]
        for day in range(30):
            assert [sum(slot[p] for slot in table['net'][day]) for p in range(9)] == days[source['seat']][day]
            for slot in range(6):
                assert [a-b for a,b in zip(table['sell'][day][slot], table['buy'][day][slot])] == table['net'][day][slot]
        samples.append(dict(sample_id=source['sample_id'], **table))
    save(output, dict(episode=group['episode'], passed=True, input_sha256=group['sha256'], samples=samples))


def main():
    inputs = load(PARENT/'INPUTS.json')
    (HERE/'episodes').mkdir(exist_ok=True)
    started = time.perf_counter()
    errors = []
    with ProcessPoolExecutor(max_workers=4, initializer=initialize) as pool:
        jobs = {pool.submit(worker, g): g['episode'] for g in inputs['episodes']}
        for count, task in enumerate(as_completed(jobs), 1):
            try:
                task.result()
            except Exception as exc:
                errors.append(dict(episode=jobs[task],error=repr(exc)))
            if count%100 == 0 or errors or count == len(jobs):
                progress = dict(completed=count,total=len(jobs),errors=errors,seconds=time.perf_counter()-started)
                save(HERE/'PROGRESS.json', progress)
                print(json.dumps(progress),flush=True)
    assert not errors
    rows = {}
    for group in inputs['episodes']:
        for sample in load(HERE/'episodes'/f"{group['episode']}.json")['samples']:
            assert sample['sample_id'] not in rows
            rows[sample['sample_id']] = {k:v for k,v in sample.items() if k!='sample_id'}
    assert len(rows) == len(inputs['sources'])
    save(HERE/'FILLS_4H.json', rows)
    save(HERE/'VERIFICATION.json', dict(passed=True,episodes=len(inputs['episodes']),samples=len(rows),
         interpreter_transitions=len(inputs['episodes'])*719,
         daily_net_identity_checks=len(rows)*30*9,
         slot_net_identity_checks=len(rows)*30*6*9,
         input='Offline private inventories and full actions used only to reconstruct labels. Predictor features remain public-only.',
         full_next_state_checks=True))


if __name__ == '__main__':
    main()
