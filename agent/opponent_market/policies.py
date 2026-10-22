"""Isolated source policies and immutable demonstration-route hypotheses."""
from copy import deepcopy
import importlib.util
import itertools
import json
from pathlib import Path
import zlib

from top1_planner.programmes import ProductionProgramme
from top1_planner.recipe import RecipeCompiler

_NAMES = itertools.count()


class SourcePolicy:
    """Rebuild a fork from compressed past inputs, without cloning code globals.

    Past inputs contain only public data and this hypothesis's private ledger.
    Modules and their internal controller state are independent between forks.
    """
    def __init__(self, path, *, history=False):
        self.path = Path(path)
        spec = importlib.util.spec_from_file_location('rival_source_'+str(next(_NAMES)), self.path)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.keep_history = history
        self.history = []
        self.configuration = None

    def __call__(self, observation, configuration):
        if self.keep_history:
            self.history.append(zlib.compress(json.dumps(observation, separators=(',', ':')).encode(), 1))
            self.configuration = dict(configuration)
        return self.module.agent(observation, configuration)

    def fork(self):
        if not self.keep_history:
            raise ValueError('Source policy fork requires its hypothesis-only history')
        child = SourcePolicy(self.path)
        for frame in self.history:
            child(json.loads(zlib.decompress(frame)), self.configuration)
        return child


class TapePolicy:
    def __init__(self, actions):
        if len(actions) != 719:
            raise ValueError('A fixed hypothesis must contain all 719 action slots')
        self.actions = actions

    def __call__(self, observation, configuration):
        return deepcopy(self.actions[int(observation['step'])])

    def fork(self):
        return TapePolicy(self.actions)


class ProgrammePolicy:
    def __init__(self, data, configuration):
        self.controller = RecipeCompiler(ProductionProgramme.from_dict(data), configuration,
                                        settlement=False, bind_herd=True, adaptive_crops=False)

    def __call__(self, observation, configuration):
        return self.controller.action(observation)

    def fork(self):
        child = object.__new__(ProgrammePolicy)
        child.controller = self.controller.fork()
        return child
