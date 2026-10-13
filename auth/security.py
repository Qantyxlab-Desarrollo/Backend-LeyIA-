import time
from typing import Optional

import jwt
from jwt import PyJWKClient, InvalidTokenError
from fastapi import HTTPException, Request, status

from .config import settings

_JWKS_CLIENTS: dict[str, PyJWKClient] = {}
CENTRAL_ISSUER = settings.central_issuer
CENTRAL_AUDIENCE = "authenticated"


def _jwks_for(issuer: str) -> PyJWKClient:
    if issuer not in _JWKS_CLIENTS:
        _JWKS_CLIENTS[issuer] = PyJWKClient(
            f"{issuer}/.well-known/jwks.json", cache_keys=True
        )
    return _JWKS_CLIENTS[issuer]


def decode_supabase_token(token: str) -> dict:
    """
    Decodifica un JWT de acceso emitido ÚNICAMENTE por el proyecto
    Supabase central de LeyIA. Rechaza tokens de cualquier otro emisor.
    """
    try:
        header = jwt.get_unverified_header(token)
        alg = header.get("alg", "")

        if alg == "HS256":
            if not settings.CENTRAL_SUPABASE_JWT_SECRET:
                raise InvalidTokenError("HS256 no habilitado en este proyecto")
            payload = jwt.decode(
                token,
                settings.CENTRAL_SUPABASE_JWT_SECRET,
                algorithms=["HS256"],
                audience=CENTRAL_AUDIENCE,
                issuer=CENTRAL_ISSUER,
            )
        elif alg in ("RS256", "ES256", "EdDSA"):
            unverified = jwt.decode(token, options={"verify_signature": False})
            issuer = (unverified.get("iss") or "").rstrip("/")
            if issuer != CENTRAL_ISSUER:
                raise InvalidTokenError("Emisor no autorizado")
            key = _jwks_for(issuer).get_signing_key_from_jwt(token).key
            payload = jwt.decode(
                token,
                key,
                algorithms=[alg],
                audience=CENTRAL_AUDIENCE,
                issuer=CENTRAL_ISSUER,
            )
        else:
            raise InvalidTokenError(f"Algoritmo {alg} no soportado")

        # Solo access tokens de usuarios autenticados
        if payload.get("role") not in (None, "authenticated"):
            raise InvalidTokenError("Rol de token inválido")
        return payload

    except jwt.ExpiredSignatureError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token expirado")
    except InvalidTokenError as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"Token inválido: {e}")
