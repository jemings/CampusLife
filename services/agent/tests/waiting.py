import asyncio
import time


async def until(condition, within_s=10.0):
    """Wait until the async `condition` holds, failing the test once `within_s` seconds pass without it."""
    deadline = time.monotonic() + within_s
    while not await condition():
        assert time.monotonic() < deadline, "the condition did not come about in time"
        await asyncio.sleep(0.02)
