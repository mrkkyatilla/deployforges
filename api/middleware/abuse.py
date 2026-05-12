from __future__ import annotations

import logging

from fastapi import Depends, HTTPException

from api.middleware.auth import AuthenticatedUser, get_current_user
from core.abuse_protection import AbuseProtection

logger = logging.getLogger(__name__)


async def check_abuse(
    user: AuthenticatedUser = Depends(get_current_user),
) -> None:
    """FastAPI dependency that blocks abusive project-creation requests."""
    protector = AbuseProtection()
    try:
        result = await protector.check_can_create_project(user.user_id, user.tier)
    except Exception:
        logger.exception("Abuse check failed for user %s — allowing by default", user.user_id)
        return
    finally:
        await protector.close()

    if not result.allowed:
        logger.warning(
            "Abuse check blocked user %s: %s (severity=%s)",
            user.user_id, result.reason, result.severity,
        )
        raise HTTPException(
            status_code=429,
            detail={
                "error": result.reason,
                "retry_after": result.retry_after,
            },
            headers={"Retry-After": str(result.retry_after or 60)},
        )
