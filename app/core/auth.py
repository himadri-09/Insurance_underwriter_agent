"""
Auth dependency: validates Supabase access token from the frontend.
Frontend handles signup/login UI via @supabase/ssr.
Backend validates token directly via Supabase REST API.
"""

import structlog
import httpx
from functools import lru_cache
from fastapi import Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from app.core.config import get_settings

log = structlog.get_logger()
security = HTTPBearer()


class AuthUser(BaseModel):
    id: str
    email: str = ""
    role: str = "authenticated"


@lru_cache(maxsize=1)
def get_settings_cached():
    """Cache settings to avoid repeated initialization."""
    return get_settings()


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> AuthUser:
    """
    Validate token by calling Supabase user endpoint directly.
    This avoids issues with client setup and is more reliable.
    """
    token = credentials.credentials
    settings = get_settings_cached()

    try:
        # Call Supabase REST API directly to validate token
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                f"{settings.supabase_url}/auth/v1/user",
                headers={
                    "Authorization": f"Bearer {token}",
                    "apikey": settings.supabase_key,
                },
            )

        if response.status_code == 200:
            user_data = response.json()
            return AuthUser(
                id=user_data.get("id", ""),
                email=user_data.get("email", ""),
                role=user_data.get("role", "authenticated"),
            )
        elif response.status_code == 401:
            raise HTTPException(401, "Invalid or expired token")
        else:
            log.warning(
                "auth_api_error",
                status=response.status_code,
                body=response.text[:200],
            )
            raise HTTPException(401, "Token validation failed")

    except httpx.TimeoutException:
        log.warning("auth_timeout")
        raise HTTPException(408, "Auth service timeout")
    except HTTPException:
        raise
    except Exception as e:
        log.warning("auth_failed", error=str(e), error_type=type(e).__name__)
        raise HTTPException(401, "Invalid token")