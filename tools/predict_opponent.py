"""Run the released predictor on one continuous public replay prefix."""
import argparse
import json
from pathlib import Path
import sys
from types import ModuleType

from common import ROOT, observation, read_replay, write_json


def install_reference_arithmetic():
    """Explicit portable arithmetic for inspection, not a performance-equivalent agent."""
    module = ModuleType('behavior_arithmetic')

    def weighted_targets(rows, weights):
        if len(rows) != 8 or len(weights) != 8:
            raise ValueError('Exactly eight neighbor rows and weights are required')
        width = len(rows[0])
        if any(len(row) != width for row in rows):
            raise ValueError('Neighbor target widths differ')
        result = []
        for i in range(width):
            value = 0.0
            for j in range(8):
                value = value + float(weights[j]) * float(rows[j][i])
            result.append(value)
        return tuple(result)

    module.weighted_targets = weighted_targets
    sys.modules['behavior_arithmetic'] = module


def forecast_prefix(path, seat, tick, *, python_reference=False):
    if tick % 24 or not 144 <= tick <= 648:
        raise ValueError('Choose a dawn in steps 144..648 (internal days 6..27)')
    sys.path.insert(0, str(ROOT / 'agent'))
    if python_reference:
        import os
        os.environ['FARM_PLANNER_BACKEND'] = 'python'
        install_reference_arithmetic()
    from p110_integration import Integration
    from top1_planner.corrected_supply import DualSupplyBelief
    replay, sha256 = read_replay(path)
    trace = []
    predictor = Integration(ROOT / 'agent/behavior_model', 'q7', trace)
    supply = DualSupplyBelief(replay['configuration'])
    for step in range(tick + 1):
        obs = observation(replay, step, seat)
        obs['_public_rival_cargo'] = supply.observe(obs)
        predictor.observe(obs)
        if step < tick:
            supply.remember_action(replay['steps'][step + 1][seat]['action'])
    value = predictor.predictor.forecast(obs)
    if value is None:
        raise RuntimeError('A complete valid public prefix did not produce a forecast')
    return dict(replay_sha256=sha256, seat=seat, step=tick,
        observations_consumed=tick + 1, python_reference_arithmetic=python_reference,
        forecast=value, diagnostics=trace,
        boundary='Predictor inspection on recorded observations, not policy performance. '
                 'No future frame, future shop, opponent private observation or opponent action is passed to the predictor.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--replay', type=Path, required=True)
    parser.add_argument('--seat', type=int, choices=(0, 1), required=True)
    parser.add_argument('--step', type=int, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--python-reference', action='store_true',
                        help='Use explicitly slower Python arithmetic for Windows/macOS inspection')
    args = parser.parse_args()
    if args.out.exists():
        raise FileExistsError(args.out)
    result = forecast_prefix(args.replay, args.seat, args.step, python_reference=args.python_reference)
    write_json(args.out, result)
    print(json.dumps({k: result[k] for k in ('seat', 'step', 'observations_consumed',
                                          'python_reference_arithmetic')}))


if __name__ == '__main__':
    main()

