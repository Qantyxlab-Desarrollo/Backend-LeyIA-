import logging
from typing import Optional

from fastapi import HTTPException, status

from .db import get_user_by_supabase_uid
from .models import User
from .security import decode_supabase_token

logger = logging.getLogger("auth_service")


def get_user_from_token(token: str) -> User:
    logger.debug("Validando token de Supabase central")
    payload = decode_supabase_token(token)

    supabase_uid = payload.get("sub")
    email = payload.get("email")

    if not supabase_uid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token inválido: falta identificador de usuario",
        )

    row = get_user_by_supabase_uid(supabase_uid)

    # Registro cerrado invite-only: el usuario DEBE existir previamente
    if not row:
        logger.warning(f"Token válido para {email} pero usuario no provisionado en LeyIA")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cuenta no habilitada. Contacte a LeyIA.",
        )

    if not row.get("is_active", True):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Usuario desactivado",
        )

    return User(**row)
