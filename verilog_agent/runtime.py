from __future__ import annotations

"""Runtime binding used by split team modules.

The CLI still owns configuration, shared helpers, and mutable runtime objects such as
llm and ARTIFACT_DIR. Team modules call refresh_globals before each agent invocation
so those names resolve exactly as they did when the agents lived in main.py.
"""

import sys

_core_module = None


def bind(module):
    global _core_module
    _core_module = module


def core():
    if _core_module is not None:
        return _core_module
    return sys.modules.get("__main__")


def refresh_globals(target):
    module = core()
    if module is None:
        raise RuntimeError("Verilog agent runtime has not been bound to a core module.")
    for name, value in vars(module).items():
        if name.startswith("__"):
            continue
        target[name] = value
