from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from .config import settings
from .models import User
from .service import get_user_from_token

security = HTTPBearer(auto_error=False)


def _extract_token(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials],
) -> Optional[str]:
    token = request.cookies.get(settings.ACCESS_TOKEN_COOKIE_NAME)
    if not token and credentials:
        token = credentials.credentials
    return token


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> User:
    token = _extract_token(request, credentials)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="No se encontró token de sesión",
        )
    return get_user_from_token(token)


async def get_current_user_optional(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> Optional[User]:
    token = _extract_token(request, credentials)
    if not token:
        return None
    return get_user_from_token(token)
