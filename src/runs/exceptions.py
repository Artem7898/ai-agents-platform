"""Run coordination errors that must not be turned into node failures."""


class ExecutionLeaseLost(RuntimeError):
    """A stale worker attempted to write after its lease was replaced/expired."""


class RunStateConflict(RuntimeError):
    """The requested state transition is not legal."""


class CancellationRequested(RuntimeError):
    """An owner requested cancellation while execution was in progress."""
