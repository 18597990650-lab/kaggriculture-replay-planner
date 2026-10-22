"""Package an explicitly chosen agent directory without replacing the frozen archive."""
import argparse
import gzip
import hashlib
import json
from pathlib import Path
import tarfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--agent', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    source, target = args.agent.resolve(), args.out.resolve()
    if not (source / 'main.py').is_file():
        raise FileNotFoundError(source / 'main.py')
    if target.exists() or source in target.parents:
        raise ValueError('Use a new archive path outside the agent directory')
    target.parent.mkdir(parents=True, exist_ok=True)
    files = sorted(p for p in source.rglob('*') if p.is_file()
                   and '__pycache__' not in p.parts and p.suffix != '.pyc')
    with target.open('xb') as raw, gzip.GzipFile(fileobj=raw, mode='wb', filename='', mtime=0) as packed:
        with tarfile.open(fileobj=packed, mode='w') as archive:
            for path in files:
                if path.is_symlink():
                    raise ValueError('Symbolic links are not supported')
                info = archive.gettarinfo(str(path), arcname=path.relative_to(source).as_posix())
                info.uid = info.gid = info.mtime = 0
                info.uname = info.gname = ''
                with path.open('rb') as content:
                    archive.addfile(info, content)
    print(json.dumps(dict(files=len(files), bytes=target.stat().st_size,
                         sha256=hashlib.sha256(target.read_bytes()).hexdigest())))


if __name__ == '__main__':
    main()

