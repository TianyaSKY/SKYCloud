"""Atomic shared quotas. Reservations survive credential refresh and runtime recreation."""

import time

from fastapi import HTTPException
from redis.exceptions import RedisError

from app.infra.extensions import redis_client
from app.features.runtime_llm.tokens import positive_setting

# Reserve both request and token counters together, or reserve nothing. The
# conservative token charge uses input bytes + maximum output tokens, so a
# client cannot avoid accounting by disconnecting a stream or omitting usage.
RESERVE_SCRIPT = """
for i = 1, #KEYS, 2 do
    if tonumber(redis.call('GET', KEYS[i]) or '0') + 1 > tonumber(ARGV[1]) or
       tonumber(redis.call('GET', KEYS[i+1]) or '0') + tonumber(ARGV[2]) > tonumber(ARGV[3]) then
        return 0
    end
end
for i = 1, #KEYS, 2 do
    redis.call('INCR', KEYS[i])
    redis.call('INCRBY', KEYS[i+1], ARGV[2])
    redis.call('EXPIRE', KEYS[i], ARGV[4])
    redis.call('EXPIRE', KEYS[i+1], ARGV[4])
end
return 1
"""


def reserve(payload: dict, input_bytes: int, output_tokens: int) -> None:
    bucket = int(time.time()) // 3600
    keys = []
    for scope in (f"user:{payload['sub']}", f"workspace:{payload['workspace_id']}", f"runtime:{payload['runtime_id']}"):
        prefix = f"runtime-llm-quota:{scope}:{bucket}"
        keys.extend([prefix + ":requests", prefix + ":tokens"])
    budget = min(payload["quota"], positive_setting("RUNTIME_LLM_HOURLY_TOKEN_BUDGET", 100000))
    try:
        accepted = redis_client.eval(
            RESERVE_SCRIPT, len(keys), *keys,
            positive_setting("RUNTIME_LLM_HOURLY_REQUEST_LIMIT", 60),
            input_bytes + output_tokens, budget, 7200,
        )
    except RedisError as exc:
        raise HTTPException(status_code=503, detail="Model quota service unavailable") from exc
    if accepted != 1:
        raise HTTPException(status_code=429, detail="Runtime model quota exceeded")
