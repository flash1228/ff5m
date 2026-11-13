"""Movement UI state and commands."""

from ui.lazy import resolve_lazy_export


# The commands load with the first use of the move page, not with the geometry the controller reads at start.
_LAZY_EXPORTS = {
    "Axis": "actions",
    "MoveCommand": "actions",
}


__all__ = ("Axis", "MoveCommand")


def __getattr__(name):
    return resolve_lazy_export(
        globals(), name, _LAZY_EXPORTS, __package__)
