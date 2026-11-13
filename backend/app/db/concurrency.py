import asyncio
from collections.abc import Callable
from typing import TypeVar


T = TypeVar("T")


async def run_blocking(operation: Callable[..., T], *args, cancel_callback: Callable[[], None] | None = None, **kwargs) -> T:
    """Keep blocking database work off the loop and finish it before cleanup."""
    pending = asyncio.create_task(asyncio.to_thread(operation, *args, **kwargs))
    try:
        return await asyncio.shield(pending)
    except asyncio.CancelledError as cancelled:
        # A cancelled await does not stop its worker or release its transaction.
        if cancel_callback is not None:
            cancel_callback()
        while not pending.done():
            try:
                await asyncio.shield(pending)
            except asyncio.CancelledError:
                continue
            except BaseException:
                break
        if not pending.cancelled():
            pending.exception()
        raise cancelled
