"""Export grouped-training data without episode identifiers in runtime vectors."""
import gzip
import hashlib
import json
from pathlib import Path
from behavior_predictor import flatten
from prepare import load, save
from public_features import add_history

HERE = Path(__file__).resolve().parent


def main():
    selected = load(HERE/'SELECTED.json')
    dataset = load(HERE/'DATASET.json')
    rows = [r for r in dataset['samples'] if r['source']['split'] == 'train']
    directory = HERE/'model'
    directory.mkdir(exist_ok=False)
    days = {}
    for day in range(6, 28):
        record = dict(day=day,
            vectors=[flatten(add_history(r['public'], day), selected['parameters']['weights']) for r in rows],
            sales=[r['net_sales'][day+1:] for r in rows])
        raw = json.dumps(record, separators=(',', ':'), allow_nan=False).encode()
        packed = gzip.compress(raw, compresslevel=6, mtime=0)
        path = directory/f'day-{day:02d}.json.gz'
        path.write_bytes(packed)
        days[str(day)] = dict(file=path.name, sha256=hashlib.sha256(packed).hexdigest(), bytes=len(packed))
    metadata = dict(schema='public-behavior-retrieval-v1', parameters=selected['parameters'],
        name=selected['name'], training_samples=len(rows), products=dataset['products'], days=days,
        selected_on='development only', future_targets='actual successful net sales',
        public_inputs_only=True, test_data_included=False)
    save(directory/'MODEL.json', metadata)
    save(HERE/'MODEL_PROVENANCE.json', dict(samples=[dict(index=i, **r['source']) for i, r in enumerate(rows)],
        dataset_sha256=hashlib.sha256((HERE/'DATASET.json').read_bytes()).hexdigest(),
        selection_sha256=hashlib.sha256((HERE/'SELECTED.json').read_bytes()).hexdigest()))
    print(json.dumps(dict(samples=len(rows), days=len(days), packed_bytes=sum(d['bytes'] for d in days.values()),
                         selected=selected['name'])), flush=True)


if __name__ == '__main__':
    main()
