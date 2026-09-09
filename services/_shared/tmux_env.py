from __future__ import annotations

import os
from collections.abc import Mapping


def tmux_safe_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    result = dict(os.environ if env is None else env)
    result.pop("TMUX", None)
    result.pop("TMUX_PANE", None)
    return result
