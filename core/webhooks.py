from __future__ import annotations

import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Webhook, WebhookDelivery
from db.session import async_session_factory

logger = logging.getLogger(__name__)

SUPPORTED_EVENTS = frozenset({
    "project.queued",
    "project.analyzing",
    "project.building",
    "project.completed",
    "project.failed",
    "build.started",
    "build.succeeded",
    "build.failed",
})

_MAX_RETRIES = 3
_REQUEST_TIMEOUT = 10.0


def _compute_signature(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(
        secret.encode(), body, hashlib.sha256,
    ).hexdigest()


class WebhookDispatcher:
    """Delivers outbound webhook notifications to user-registered endpoints."""

    async def dispatch(
        self,
        user_id: UUID,
        event: str,
        data: dict,
        *,
        db: AsyncSession | None = None,
    ) -> None:
        if event not in SUPPORTED_EVENTS:
            logger.warning("Ignoring unsupported webhook event: %s", event)
            return

        owns_session = db is None
        if owns_session:
            db = async_session_factory()

        try:
            result = await db.execute(
                select(Webhook).where(
                    Webhook.user_id == user_id,
                    Webhook.is_active == 1,
                )
            )
            webhooks = result.scalars().all()

            matching = [
                wh for wh in webhooks if event in (wh.events or [])
            ]
            if not matching:
                return

            payload = {
                "event": event,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "data": data,
            }
            body = json.dumps(payload).encode()

            async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
                for wh in matching:
                    await self._deliver(client, wh, event, payload, body, db)

            if owns_session:
                await db.commit()
        except Exception:
            if owns_session:
                await db.rollback()
            raise
        finally:
            if owns_session:
                await db.close()

    async def _deliver(
        self,
        client: httpx.AsyncClient,
        webhook: Webhook,
        event: str,
        payload: dict,
        body: bytes,
        db: AsyncSession,
    ) -> None:
        headers = {
            "Content-Type": "application/json",
            "X-DeployForge-Event": event,
        }
        if webhook.secret:
            headers["X-DeployForge-Signature"] = _compute_signature(
                webhook.secret, body,
            )

        last_status: int | None = None
        last_body: str | None = None
        success = False

        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                resp = await client.post(
                    str(webhook.url), content=body, headers=headers,
                )
                last_status = resp.status_code
                last_body = resp.text[:2000]

                if 200 <= resp.status_code < 300:
                    success = True
                    self._record_delivery(
                        db, webhook.id, event, payload,
                        last_status, last_body, True, attempt,
                    )
                    logger.info(
                        "Webhook %s delivered event %s (attempt %d)",
                        webhook.id, event, attempt,
                    )
                    return

                logger.warning(
                    "Webhook %s returned %d for event %s (attempt %d/%d)",
                    webhook.id, resp.status_code, event, attempt, _MAX_RETRIES,
                )
            except httpx.TimeoutException:
                logger.warning(
                    "Webhook %s timed out for event %s (attempt %d/%d)",
                    webhook.id, event, attempt, _MAX_RETRIES,
                )
                last_status = None
                last_body = "timeout"
            except httpx.HTTPError as exc:
                logger.warning(
                    "Webhook %s network error for event %s (attempt %d/%d): %s",
                    webhook.id, event, attempt, _MAX_RETRIES, exc,
                )
                last_status = None
                last_body = str(exc)[:2000]

            if attempt < _MAX_RETRIES:
                import asyncio
                await asyncio.sleep(2 ** (attempt - 1))

        self._record_delivery(
            db, webhook.id, event, payload,
            last_status, last_body, False, _MAX_RETRIES,
        )
        logger.error(
            "Webhook %s delivery failed after %d attempts for event %s",
            webhook.id, _MAX_RETRIES, event,
        )

    @staticmethod
    def _record_delivery(
        db: AsyncSession,
        webhook_id: UUID,
        event: str,
        payload: dict,
        response_status: int | None,
        response_body: str | None,
        success: bool,
        attempt_number: int,
    ) -> None:
        delivery = WebhookDelivery(
            webhook_id=webhook_id,
            event=event,
            payload=payload,
            response_status=response_status,
            response_body=response_body,
            success=int(success),
            attempt_number=attempt_number,
        )
        db.add(delivery)


webhook_dispatcher = WebhookDispatcher()
