"""Rebuild Cython extensions in an isolated Linux CPython 3.11 candidate."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import sys
import sysconfig

from common import ROOT, load_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--jobs', type=int, default=1)
    args = parser.parse_args()
    if not (sys.implementation.name == 'cpython' and sys.version_info[:2] == (3, 11)
            and sys.platform == 'linux' and platform.machine().lower() in ('x86_64', 'amd64')):
        raise RuntimeError('Native rebuild requires Linux x86_64 CPython 3.11')
    if args.jobs < 1:
        raise ValueError('--jobs must be positive')
    output = args.out.resolve()
    if output.exists() or output == ROOT / 'agent':
        raise FileExistsError('Choose a new candidate output directory')
    from Cython.Build import cythonize
    import Cython
    from setuptools import Extension, setup
    source = ROOT / 'agent'
    previous = load_json(source / 'native_planner/cpython-311-linux-x86_64/BUILD.json')
    shutil.copytree(source, output, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    build_root = output.parent / (output.name + '-build')
    if build_root.exists():
        raise FileExistsError(build_root)
    build_root.mkdir()
    native = output / 'native_planner/cpython-311-linux-x86_64'
    suffix = sysconfig.get_config_var('EXT_SUFFIX')
    flags = ['-O3', '-fno-fast-math', '-ffp-contract=off', '-fwrapv', '-fno-strict-aliasing']
    extensions = []
    modules = [filename.split('.cpython-')[0] for filename in previous['artifacts']]
    for name in modules:
        pyx = output / 'top1_planner' / (name + '.pyx')
        path = pyx if pyx.exists() else output / 'top1_planner' / (name + '.py')
        if not path.exists():
            raise FileNotFoundError(path)
        extensions.append(Extension('top1_planner.' + name, [str(path)], extra_compile_args=flags))
    extensions.append(Extension('behavior_arithmetic', [str(output / 'behavior_arithmetic.pyx')],
                                extra_compile_args=flags))
    directives = dict(previous['directives'])
    os.chdir(output)
    setup(name='kaggriculture-native-rebuild',
          ext_modules=cythonize(extensions, compiler_directives=directives,
                                include_path=[str(output)], build_dir=str(build_root / 'cython'),
                                nthreads=args.jobs),
          script_args=['build_ext', '--build-lib', str(build_root / 'lib'),
                       '--build-temp', str(build_root / 'obj'), '--parallel', str(args.jobs)])
    for name in modules:
        shutil.copy2(build_root / 'lib/top1_planner' / (name + suffix), native / (name + suffix))
    shutil.copy2(build_root / 'lib' / ('behavior_arithmetic' + suffix),
                 output / ('behavior_arithmetic' + suffix))
    digest = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
    manifest = dict(python=sys.version, cython=Cython.__version__, platform=platform.platform(),
        extension_suffix=suffix, directives=directives,
        sources={name: digest(output / name) for name in previous['sources']},
        artifacts={name + suffix: digest(native / (name + suffix)) for name in modules},
        optimization={'pgo': False, 'flags': flags},
        boundary='Ordinary optimized rebuild, not a bit-identical reproduction of the original PGO binaries')
    (native / 'BUILD.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    print(json.dumps(dict(candidate=str(output), rebuilt_extensions=len(extensions), pgo=False)))


if __name__ == '__main__':
    main()
