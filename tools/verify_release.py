"""Verify the original artifact, expanded runtime, native hashes, and library rebuild."""
import ast
import hashlib
import json
from pathlib import Path, PurePosixPath
import tarfile
import tempfile

from common import ROOT, load_json
from build_library import build


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def verify():
    release = load_json(ROOT / 'RELEASE.json')
    if digest(ROOT / 'submission.tar.gz') != release['sha256']:
        raise AssertionError('Submission archive hash differs')
    with tarfile.open(ROOT / 'submission.tar.gz', 'r:gz') as archive:
        members = archive.getmembers()
        names = [m.name for m in members]
        if len(names) != len(set(names)) or set(names) != set(release['agent_files']):
            raise AssertionError('Unexpected submission archive member set')
        for member in members:
            name = PurePosixPath(member.name)
            if not member.isfile() or name.is_absolute() or '..' in name.parts:
                raise ValueError('Unsafe archive member')
            raw = archive.extractfile(member).read()
            if hashlib.sha256(raw).hexdigest() != release['agent_files'][member.name]:
                raise AssertionError(member.name)
            if raw != (ROOT / 'agent' / member.name).read_bytes():
                raise AssertionError('Expanded source differs: ' + member.name)
    for name, expected in release['reference_files'].items():
        if digest(ROOT / 'reference' / name) != expected:
            raise AssertionError(name)
    compiled = load_json(ROOT / 'agent/native_planner/cpython-311-linux-x86_64/BUILD.json')
    for name, expected in compiled['sources'].items():
        if digest(ROOT / 'agent' / name) != expected:
            raise AssertionError('Compiled source mismatch: ' + name)
    for name, expected in compiled['artifacts'].items():
        if digest(ROOT / 'agent/native_planner/cpython-311-linux-x86_64' / name) != expected:
            raise AssertionError('Compiled binary mismatch: ' + name)
    with tempfile.TemporaryDirectory(prefix='kaggriculture-library-') as temporary:
        target = Path(temporary) / 'programmes'
        rebuilt = build(ROOT / 'data/library_config.json', ROOT / 'data/source_routes.json',
                        ROOT / 'data/raw_programmes', target)
        for path in target.glob('*.json'):
            if path.read_bytes() != (ROOT / 'agent/programmes' / path.name).read_bytes():
                raise AssertionError('Library rebuild mismatch: ' + path.name)
        count = len(list(target.glob('*.json')))
    python_files = [p for parent in ('agent', 'tools', 'reference', 'research_sources', 'tests')
                    for p in (ROOT / parent).rglob('*.py')]
    for path in python_files:
        ast.parse(path.read_text(encoding='utf-8-sig'), filename=str(path))
    return dict(passed=True, archive_sha256=release['sha256'], agent_files_verified=len(members),
        rebuilt_programmes=rebuilt['programmes'], rebuilt_library_files_byte_identical=count,
        native_artifacts_verified=len(compiled['artifacts']), python_files_parsed=len(python_files),
        boundary='Integrity and reconstruction checks only; not a new complete-game or online evaluation')


if __name__ == '__main__':
    print(json.dumps(verify(), indent=2))

