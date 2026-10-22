"""Inventory the two exact teacher submissions without selecting by outcome."""
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
import hashlib
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
SOURCES = {
    56686734: ROOT / 'research/all-requested-opponents-latest50-20260927/submission-56686734',
    56686742: ROOT / 'research/all-requested-opponents-latest50-20260927/submission-56686742',
}
TICKS = (0, 24, 48, 72, 96, 120, 144, 168, 192, 216, 240, 288, 360, 432, 504, 576, 648, 719)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def scan(job):
    submission, record = job
    raw = Path(record['path']).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == record['sha256'], record['episode_id']
    replay = json.loads(raw)
    assert len(replay['steps']) == 720 and all(s == 'DONE' for s in replay['statuses'])
    seats = [r['seat'] for r in record['actors'] if r['submission_id'] == submission]
    assert seats == record['our_seats'] and len(seats) == 1
    seat = seats[0]
    assert record['actors'][seat]['team'] == 'Boey'
    assert replay['rewards'][seat] == record['actors'][seat]['reward']
    snapshots = {}
    for tick in TICKS:
        obs = dict(replay['steps'][tick][0]['observation'], **replay['steps'][tick][seat]['observation'])
        farm = obs['farms'][seat]
        layout = []
        for y, line in enumerate(farm['tiles']):
            for x, tile in enumerate(line):
                if isinstance(tile, dict) and (tile.get('animal') or tile.get('crop')):
                    layout.append([x, y, tile.get('animal') or tile['crop'],
                        tile.get('placed_day') if tile.get('animal') else tile['planted_day']])
        snapshots[str(tick)] = dict(assets=dict(Counter(row[2] for row in layout)), layout=layout,
            layout_hash=digest(layout), land=farm['unlocked_quadrants'], cash=farm['money'],
            shops=obs['town']['unlocked_shops'], prices=obs['market']['prices'])
    workers = [0] * 30
    for tick, frame in enumerate(replay['steps']):
        obs = dict(frame[0]['observation'], **frame[seat]['observation'])
        workers[tick // 24] = max(workers[tick // 24], len(obs['farms'][seat]['hands']))
    prefix = [frame[seat]['action'] for frame in replay['steps'][1:73]]
    return dict(episode=record['episode_id'], submission=submission, seat=seat,
        path=record['path'], sha256=record['sha256'], margin=record['margin'],
        outcome=record['outcome'], opponent=record['actors'][1-seat],
        snapshots=snapshots, workers=workers, action_prefix72=digest(prefix))


def main():
    target = HERE / 'REPLAY_INDEX.json'
    if target.exists():
        raise FileExistsError(target)
    jobs, manifests = [], {}
    for submission, directory in SOURCES.items():
        data = json.loads((directory / 'MANIFEST.json').read_bytes())
        assert data['submission_id'] == submission and data['team'] == 'Boey'
        assert data['status'] == 'COMPLETE' and not data['errors']
        manifests[str(submission)] = {key: data[key] for key in (
            'captured_at_utc', 'completed_at_utc', 'downloaded_and_verified', 'outcomes')}
        jobs.extend((submission, record) for record in data['records'])
    rows = []
    with ProcessPoolExecutor(max_workers=4) as pool:
        for future in as_completed([pool.submit(scan, job) for job in jobs]):
            rows.append(future.result())
            if len(rows) % 40 == 0:
                print(json.dumps(dict(checked=len(rows), total=len(jobs))), flush=True)
    rows.sort(key=lambda row: (row['submission'], row['episode']))
    summary = {}
    for submission in SOURCES:
        subset = [row for row in rows if row['submission'] == submission]
        summary[str(submission)] = dict(games=len(subset),
            shops72=dict(Counter(row['snapshots']['72']['shops'][0] for row in subset)),
            paid72=dict(Counter(row['snapshots']['72']['layout_hash'] for row in subset)),
            paid144=dict(Counter(row['snapshots']['144']['layout_hash'] for row in subset)),
            land719=dict(Counter(str(row['snapshots']['719']['land']) for row in subset)))
    target.write_text(json.dumps(dict(sources=manifests, summary=summary, rows=rows,
        boundary='Frozen local public archives; counts are at the stated captures, not a current leaderboard query. '
                 'Hashes, teacher seats and rewards verified here; native action audit follows for selected routes.'),
        ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: dict(games=v['games'], openings=len(v['paid72']), land=v['land719']) for k,v in summary.items()}), flush=True)


if __name__ == '__main__':
    main()
