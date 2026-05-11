"""
Auth dependency: validates Supabase access token from the frontend.
Frontend handles signup/login UI via @supabase/ssr.
Backend just asks Supabase "is this token valid?" — no JWT secret needed.
"""

import structlog
from fastapi import Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from supabase import create_client
from app.core.config import get_settings

log = structlog.get_logger()
security = HTTPBearer()


class AuthUser(BaseModel):
    id: str
    email: str = ""
    role: str = "authenticated"


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> AuthUser:
    """
    Validate token by calling supabase.auth.get_user(token).
    Supabase does the validation — we just need the URL and anon key.
    """
    token = credentials.credentials
    settings = get_settings()

    try:
        sb = create_client(settings.supabase_url, settings.supabase_key)
        response = sb.auth.get_user(token)

        if not response or not response.user:
            raise HTTPException(401, "Invalid or expired token")

        return AuthUser(
            id=response.user.id,
            email=response.user.email or "",
            role=response.user.role or "authenticated",
        )

    except HTTPException:
        raise
    except Exception as e:
        log.warning("auth_failed", error=str(e))
        raise HTTPException(401, "Invalid token")