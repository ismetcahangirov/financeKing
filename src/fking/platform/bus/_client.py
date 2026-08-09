"""Redis client construction. One place, one set of options.

Not routed through `fking.platform.safety.guarded_client()`, and the reason is worth
stating rather than assuming. The host allowlist exists to make it impossible to address
a *production exchange* -- it is about where orders could go. Redis is local
infrastructure inside the Compose network, holds no venue credentials and accepts no
order. Adding `redis://` hosts to the trading allowlist to satisfy a symmetry would
widen the one set whose narrowness is the system's central guarantee, which is exactly
the move `docs/rules/safety-kernel.md` refuses.

What does apply is the import contract: only `fking.platform` may construct a Redis
client, so a consumer in `fking.execution` cannot quietly open its own connection with
different decoding or no health check.
"""

from __future__ import annotations

from redis.asyncio import Redis

from fking.platform.config.settings import BusSettings


def build_redis(settings: BusSettings) -> Redis:
    """A Redis client for the event bus.

    `decode_responses=True` because every value on these streams is a JSON string this
    package wrote. Leaving it off would push a `bytes`/`str` branch into every consumer,
    and the branch would be got wrong somewhere.

    `health_check_interval` is set because Redis closes idle connections and a consumer
    blocked in `XREADGROUP` between sparse events is idle by definition -- without it the
    first event after a quiet period surfaces as a connection error rather than as an
    event.
    """
    return Redis.from_url(
        str(settings.redis_url),
        decode_responses=True,
        health_check_interval=30,
        # A blocked XREADGROUP must not be killed by the socket timeout. The block
        # duration is passed per call and is always well under this.
        socket_timeout=None,
        socket_connect_timeout=5,
    )
