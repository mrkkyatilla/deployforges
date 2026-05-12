import hashlib
from datetime import datetime, timezone
from uuid import UUID

from fastapi import Depends, HTTPException, Security
from fastapi.security import APIKeyHeader
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from db.models import APIKey, User
from db.session import get_db

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


class AuthenticatedUser:
    """Authenticated user context passed through dependency injection."""

    def __init__(self, user_id: UUID, email: str, tier: str, credits_balance: float):
        self.user_id = user_id
        self.email = email
        self.tier = tier
        self.credits_balance = credits_balance


def hash_api_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode()).hexdigest()


async def get_current_user(
    api_key: str | None = Security(api_key_header),
    db: AsyncSession = Depends(get_db),
) -> AuthenticatedUser:
    if not api_key:
        raise HTTPException(status_code=401, detail="Missing API key")

    if not api_key.startswith("df_"):
        raise HTTPException(status_code=401, detail="Invalid API key format")

    key_hash = hash_api_key(api_key)

    result = await db.execute(
        select(APIKey)
        .options(joinedload(APIKey.user))
        .where(APIKey.key_hash == key_hash, APIKey.is_active == 1)
    )
    api_key_record = result.scalar_one_or_none()

    if not api_key_record:
        raise HTTPException(status_code=401, detail="Invalid or revoked API key")

    api_key_record.last_used_at = datetime.now(timezone.utc)

    user = api_key_record.user
    return AuthenticatedUser(
        user_id=user.id,
        email=user.email,
        tier=user.tier,
        credits_balance=user.credits_balance,
    )
