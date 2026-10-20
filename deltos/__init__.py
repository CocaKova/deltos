"""deltos: memory that knows what is true now, and what it used to be.

    from deltos import Memory
    m = Memory()                 # ~/deltos (or $DELTOS_ROOT), configured by deltos.toml
    m.context(message)           # the per-turn memory block
    m.call(tool, args)           # the tools an agent gets (m.tools, m.instructions())
"""
from .memory import Memory, Refused

__all__ = ["Memory", "Refused"]
__version__ = "0.1.0"
