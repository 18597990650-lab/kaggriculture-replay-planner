"""BOEY02: isolated Boey production schedules on the P110 physical engine."""
import importlib.util
from pathlib import Path
import sys
HERE = Path(sys._getframe().f_code.co_filename).resolve().parent
sys.path.insert(0, str(HERE))
spec = importlib.util.spec_from_file_location('supply35_parent', HERE / 'parent_entry.py')
_PARENT = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = _PARENT
spec.loader.exec_module(_PARENT)
from search_policy import choose_programme
from ordinary_cohort_queue import ordinary_response
_PARENT._RUNTIME.ordinary_service_response=ordinary_response
_PARENT._CHOOSE = choose_programme
from whole_library_bridge import install
install(_PARENT)
def get_trace_records():
    return _PARENT.get_trace_records()
def agent(observation, configuration):
    return _PARENT.agent(observation, configuration)
