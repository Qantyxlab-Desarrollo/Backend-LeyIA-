import time

import httpx
from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel

from .config import settings
from .dependencies import get_current_user
from .models import User
from .security import decode_supabase_token

router = APIRouter(prefix="/api/auth", tags=["auth"])


class ProfileResponse(BaseModel):
    success: bool
    user: dict


class TokenBody(BaseModel):
    access_token: str


@router.get("/me", response_model=ProfileResponse)
async def me(user: User = Depends(get_current_user)):
    return ProfileResponse(success=True, user=user.model_dump())


@router.post("/session")
async def create_session(body: TokenBody, response: Response):
    """
    El frontend envía el access_token obtenido de Supabase Auth
    y el backend lo guarda en una cookie httpOnly.
    """
    payload = decode_supabase_token(body.access_token)
    exp = payload.get("exp")
    max_age = max(0, int(exp - time.time())) if exp else 3600

    response.set_cookie(
        key=settings.ACCESS_TOKEN_COOKIE_NAME,
        value=body.access_token,
        httponly=True,
        secure=settings.SECURE_COOKIES,
        samesite="lax",
        domain=settings.COOKIE_DOMAIN,
        path="/",
        max_age=max_age,
    )
    return {"success": True}


@router.post("/logout")
async def logout(request: Request, response: Response):
    """
    Revoca los refresh tokens en Supabase Auth y borra la cookie.
    El access token sigue válido hasta su expiración natural,
    pero el flag is_active local permite revocación inmediata.
    """
    token = request.cookies.get(settings.ACCESS_TOKEN_COOKIE_NAME)
    if token and settings.CENTRAL_SUPABASE_ANON_KEY:
        try:
            await httpx.AsyncClient().post(
                f"{settings.CENTRAL_SUPABASE_URL}/auth/v1/logout",
                headers={
                    "Authorization": f"Bearer {token}",
                    "apikey": settings.CENTRAL_SUPABASE_ANON_KEY,
                },
                timeout=5,
            )
        except Exception:
            pass  # no fallar el logout por un error de red

    response.delete_cookie(
        key=settings.ACCESS_TOKEN_COOKIE_NAME,
        domain=settings.COOKIE_DOMAIN,
        path="/",
    )
    return {"success": True, "message": "Sesión cerrada"}


@router.get("/me/tenants")
async def me_tenants():
    """
    Placeholder: en v1 un usuario = un tenant.
    En v2 se devuelven las membresías activas del usuario.
    """
    return {"success": True, "tenants": []}
