"""Python 3.12 compatibility for the HALucinator submodule, applied in-process.

HALucinator targets older Pythons; rather than patch the submodule we restore
the few removed stdlib APIs it calls before importing it.
"""

import inspect
from collections import namedtuple

if not hasattr(inspect, "getargspec"):  # removed in 3.11; used by hal_config
    ArgSpec = namedtuple("ArgSpec", "args varargs keywords defaults")

    def getargspec(func):
        s = inspect.getfullargspec(func)
        return ArgSpec(s.args, s.varargs, s.varkw, s.defaults)

    inspect.getargspec = getargspec
