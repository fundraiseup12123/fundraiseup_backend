"""
Background scheduler to process daily renewals for PayPal vault subscriptions.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)

_scheduler_task: Optional[asyncio.Task] = None
_RUN_INTERVAL_SECONDS = 3600  # Check every hour for due renewals


async def _renewal_loop() -> None:
    logger.info("[paypal_scheduler] Background renewal loop started.")
    while True:
        try:
            from routers.paypal import process_paypal_vault_renewals
            renewals = process_paypal_vault_renewals()
            if renewals:
                logger.info("[paypal_scheduler] Processed %d renewals: %s", len(renewals), renewals)
        except asyncio.CancelledError:
            logger.info("[paypal_scheduler] Background renewal loop cancelled.")
            break
        except Exception as exc:
            logger.error("[paypal_scheduler] Error in renewal loop: %s", exc, exc_info=True)

        try:
            await asyncio.sleep(_RUN_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            break


def start_paypal_scheduler() -> None:
    global _scheduler_task
    if _scheduler_task is None or _scheduler_task.done():
        try:
            loop = asyncio.get_running_loop()
            _scheduler_task = loop.create_task(_renewal_loop())
            logger.info("[paypal_scheduler] Started PayPal renewal background task.")
        except RuntimeError:
            logger.warning("[paypal_scheduler] No running event loop to start PayPal scheduler.")


def stop_paypal_scheduler() -> None:
    global _scheduler_task
    if _scheduler_task and not _scheduler_task.done():
        _scheduler_task.cancel()
        _scheduler_task = None
        logger.info("[paypal_scheduler] Stopped PayPal renewal background task.")
