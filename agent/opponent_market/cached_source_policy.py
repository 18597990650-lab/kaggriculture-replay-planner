"""Explicit state forks for the audited immutable public-V5 source version."""
from copy import deepcopy
import hashlib
from pathlib import Path

from .policies import SourcePolicy
from .compiled_source_loading import load_source

SOURCE_SHA256 = 'a2047ebd8ca5720221e1421529655d9c67a7b2fedb74e874c7d3c55a8970ac7e'
STATE_MAPS = (
    '_TERMINAL_PLANS','_TERMINAL_PREVIOUS','_UPGRADE_STATS','_ROOM_STATS',
    '_V219_STATES','_V219_REPORT','_V231_STATES','_V231_REPORT','_R36_SALE_REPORT',
    '_R44_PROBES','_R44_REPORT','_R37_PLAYERS','_R37_HORIZONS','_R37_REPORT','_R37_STATS',
    '_RELEASE_REPORT','_V233_STATES','_V233_REPORT','_R46_REPORT',
    '_R51_INPUT_STATES','_R51_INPUT_REPORT','_R51_WAREHOUSE_REPORT',
    '_R53_LABOR_REPORT','_R53_LABOR_COMBINED','_R70_STATES','_R70_REPORT',
    '_R85_STATES','_R85_REPORT','_R86_FEED_CACHE',
)
CHASSIS_MAPS = ('players','diagnostics','_future_sells','cfg')


def _plain_data(value, seen):
    if value is None or type(value) in (str,int,float,bool):
        return
    if id(value) in seen:
        return
    seen.add(id(value))
    if type(value) is dict:
        for key,member in value.items():
            _plain_data(key,seen)
            _plain_data(member,seen)
    elif type(value) in (tuple,list,set,frozenset):
        for member in value:
            _plain_data(member,seen)
    else:
        raise TypeError('Unsupported source-state object: '+type(value).__name__)


def state_snapshot(policy):
    module = policy.module
    bundle = {name:getattr(module,name) for name in STATE_MAPS}
    chassis = module._IMPL.chassis
    bundle['chassis'] = {name:getattr(chassis,name) for name in CHASSIS_MAPS}
    bundle['release_errors'] = module._RELEASE_ERRORS
    _plain_data(bundle,set())
    return deepcopy(bundle)


class CachedSourcePolicy(SourcePolicy):
    """Copy declared data state into fresh code globals and fresh closures.

    Reports are updated in place so existing function telemetry aliases remain
    bound to their own source module. A shared deepcopy memo preserves aliases
    between global maps and the chassis. No parent functions or controllers are
    copied into a child code namespace.
    """
    def __init__(self,path,*,history=False):
        if hashlib.sha256(Path(path).read_bytes()).hexdigest()!=SOURCE_SHA256:
            raise ValueError('The fast state adapter only supports its audited source hash')
        self.path = Path(path)
        self.module = load_source(self.path)
        self.keep_history = history
        self.history = []
        self.configuration = None

    def fork(self):
        child = CachedSourcePolicy(self.path)
        pairs = [(getattr(self.module,name),getattr(child.module,name),name) for name in STATE_MAPS]
        parent_chassis,child_chassis = self.module._IMPL.chassis,child.module._IMPL.chassis
        pairs.extend((getattr(parent_chassis,name),getattr(child_chassis,name),'chassis.'+name)
                     for name in CHASSIS_MAPS)
        _plain_data([left for left,_,_ in pairs],set())
        memo, reverse, unique = {},{},[]
        for left,right,name in pairs:
            if type(left) is not dict or type(right) is not dict:
                raise TypeError('Source-state map changed type: '+name)
            if id(left) in memo:
                if memo[id(left)] is not right:
                    raise ValueError('Source aliases differ from fresh code: '+name)
                continue
            if id(right) in reverse and reverse[id(right)] != id(left):
                raise ValueError('Fresh code aliases differ from source: '+name)
            memo[id(left)] = right
            reverse[id(right)] = id(left)
            unique.append((left,right))
        for _,right in unique:
            right.clear()
        for left,right in unique:
            for key,value in left.items():
                right[deepcopy(key,memo)] = deepcopy(value,memo)
        if type(self.module._RELEASE_ERRORS) is not int:
            raise TypeError('The release error counter changed type')
        child.module._RELEASE_ERRORS = self.module._RELEASE_ERRORS
        return child
