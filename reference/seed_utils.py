from __future__ import annotations
import random
from typing import Any, Callable

def resolve_episode_seed(
    env: Any,
    *,
    config_key: str = "seed",
    fallback: Callable[[], int] | None = None,
) -> int:
    """Resolve, scrub, and persist an episode seed for envs with hidden state.

    Used by interpreters whose initial state depends on a seed that agents
    must not be able to read (e.g. random maze layout, comet schedules,
    weather rolls). The seed is taken from the first available source:
    ``env.info["seed"]`` (preserved across re-initialization), then
    ``configuration[config_key]``, then ``fallback()`` (defaulting to a random
    31-bit int). The value is then cleared from ``configuration`` so agents
    can't read it via the observation, and stored on ``env.info["seed"]`` so
    it persists into the replay.
    """
    if not hasattr(env, "info") or env.info is None:
        env.info = {}
    seed = env.info.get("seed")
    config = env.configuration
    if seed is None:
        seed = getattr(config, config_key, None)
        if seed is None and isinstance(config, dict):
            seed = config.get(config_key)
    if seed is None:
        seed = fallback() if fallback is not None else random.randrange(2**31)
    try:
        setattr(config, config_key, None)
    except (AttributeError, TypeError):
        config[config_key] = None
    env.info["seed"] = seed
    return seed
