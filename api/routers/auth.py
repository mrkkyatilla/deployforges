from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.middleware.auth import AuthenticatedUser, get_current_user, hash_api_key
from db.models import APIKey, User
from db.session import get_db

router = APIRouter(prefix="/auth", tags=["auth"])


class RegisterRequest(BaseModel):
    email: EmailStr


class RegisterResponse(BaseModel):
    user_id: str
    email: str
    api_key: str
    tier: str
    credits_balance: float
    message: str


class CreateKeyRequest(BaseModel):
    name: str = Field(default="default", max_length=100)


class APIKeyResponse(BaseModel):
    id: str
    name: str
    key_prefix: str
    created_at: datetime


class APIKeyCreatedResponse(BaseModel):
    id: str
    name: str
    api_key: str
    key_prefix: str
    message: str


class KeyListResponse(BaseModel):
    data: list[APIKeyResponse]


def _generate_api_key() -> str:
    random_part = secrets.token_hex(24)
    return f"df_live_{random_part}"


@router.post("/register", response_model=RegisterResponse, status_code=201)
async def register(
    payload: RegisterRequest,
    db: AsyncSession = Depends(get_db),
) -> RegisterResponse:
    result = await db.execute(select(User).where(User.email == payload.email))
    existing = result.scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=409, detail="Email already registered")

    user = User(
        id=uuid.uuid4(),
        email=payload.email,
        tier="free",
        credits_balance=5.0,
    )
    db.add(user)
    await db.flush()

    raw_key = _generate_api_key()
    key_record = APIKey(
        id=uuid.uuid4(),
        user_id=user.id,
        key_hash=hash_api_key(raw_key),
        key_prefix=raw_key[:12],
        name="default",
        is_active=1,
    )
    db.add(key_record)

    return RegisterResponse(
        user_id=str(user.id),
        email=user.email,
        api_key=raw_key,
        tier=user.tier,
        credits_balance=user.credits_balance,
        message="Registration successful. Save your API key — it won't be shown again.",
    )


@router.post("/keys", response_model=APIKeyCreatedResponse, status_code=201)
async def create_api_key(
    payload: CreateKeyRequest,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> APIKeyCreatedResponse:
    result = await db.execute(
        select(APIKey).where(APIKey.user_id == user.user_id, APIKey.is_active == 1)
    )
    active_keys = result.scalars().all()
    if len(active_keys) >= 5:
        raise HTTPException(status_code=400, detail="Maximum 5 active API keys allowed")

    raw_key = _generate_api_key()
    key_record = APIKey(
        id=uuid.uuid4(),
        user_id=user.user_id,
        key_hash=hash_api_key(raw_key),
        key_prefix=raw_key[:12],
        name=payload.name,
        is_active=1,
    )
    db.add(key_record)
    await db.flush()

    return APIKeyCreatedResponse(
        id=str(key_record.id),
        name=key_record.name,
        api_key=raw_key,
        key_prefix=raw_key[:12],
        message="API key created. Save it — it won't be shown again.",
    )


@router.get("/keys", response_model=KeyListResponse)
async def list_api_keys(
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> KeyListResponse:
    result = await db.execute(
        select(APIKey)
        .where(APIKey.user_id == user.user_id, APIKey.is_active == 1)
        .order_by(APIKey.created_at.desc())
    )
    keys = result.scalars().all()
    return KeyListResponse(
        data=[
            APIKeyResponse(
                id=str(k.id),
                name=k.name,
                key_prefix=k.key_prefix,
                created_at=k.created_at,
            )
            for k in keys
        ]
    )


@router.delete("/keys/{key_id}", status_code=204)
async def revoke_api_key(
    key_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    result = await db.execute(
        select(APIKey).where(
            APIKey.id == uuid.UUID(key_id),
            APIKey.user_id == user.user_id,
        )
    )
    key_record = result.scalar_one_or_none()
    if not key_record:
        raise HTTPException(status_code=404, detail="API key not found")

    key_record.is_active = 0


@router.get("/me")
async def get_current_user_info(
    user: AuthenticatedUser = Depends(get_current_user),
):
    return {
        "user_id": str(user.user_id),
        "email": user.email,
        "tier": user.tier,
        "credits_balance": user.credits_balance,
    }
