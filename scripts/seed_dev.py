"""Seed development database with a test user and API key."""

from __future__ import annotations

import asyncio
import hashlib
import os
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

# VPS / Docker: set DF_DATABASE_URL (same as API). Local default matches docker-compose.yml db.
DATABASE_URL = os.environ.get(
    "DF_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5432/deployforge",
)

TEST_USER_EMAIL = "dev@deployforge.local"
TEST_API_KEY = "df_live_dev_test_key_for_local_development_only"


async def seed():
    engine = create_async_engine(DATABASE_URL)
    try:
        async with AsyncSession(engine) as db:
            result = await db.execute(
                text("SELECT id FROM users WHERE email = :email"),
                {"email": TEST_USER_EMAIL},
            )
            existing = result.scalar_one_or_none()
            if existing:
                print(f"User already exists: {TEST_USER_EMAIL}")
                print(f"API Key: {TEST_API_KEY}")
                return

            user_id = uuid.uuid4()
            key_hash = hashlib.sha256(TEST_API_KEY.encode()).hexdigest()

            await db.execute(
                text("""
                    INSERT INTO users (id, email, tier, credits_balance)
                    VALUES (:id, :email, 'builder', 100.0)
                """),
                {"id": user_id, "email": TEST_USER_EMAIL},
            )

            await db.execute(
                text("""
                    INSERT INTO api_keys (id, user_id, key_hash, key_prefix, name, is_active)
                    VALUES (:id, :user_id, :key_hash, :key_prefix, 'dev-default', 1)
                """),
                {
                    "id": uuid.uuid4(),
                    "user_id": user_id,
                    "key_hash": key_hash,
                    "key_prefix": TEST_API_KEY[:12],
                },
            )

            await db.commit()

        print("=" * 50)
        print("  Development seed complete!")
        print("=" * 50)
        print()
        print(f"  Email:   {TEST_USER_EMAIL}")
        print(f"  Tier:    builder")
        print(f"  Credits: 100.0")
        print(f"  API Key: {TEST_API_KEY}")
        print()
        print("  Use this key in requests:")
        print(f'  curl -H "X-API-Key: {TEST_API_KEY}" http://localhost:8000/api/v1/health')
        print("=" * 50)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(seed())
