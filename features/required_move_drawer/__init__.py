"""Right-side sliding drawer of live per-option required-move sparklines."""

from features.required_move_drawer.callbacks import register_required_move_drawer_callbacks
from features.required_move_drawer.rendering import build_drawer_shell

__all__ = ["build_drawer_shell", "register_required_move_drawer_callbacks"]
