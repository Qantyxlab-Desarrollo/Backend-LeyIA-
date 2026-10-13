# core/secrets.py
from abc import ABC, abstractmethod
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

from auth.config import settings


class SecretProvider(ABC):
    @abstractmethod
    def encrypt(self, plaintext: str) -> str: ...
    @abstractmethod
    def decrypt(self, ciphertext: str) -> str: ...


class FernetSecretProvider(SecretProvider):
    def __init__(self, key: Optional[str] = None):
        key = key or getattr(settings, "TENANT_SECRETS_KEY", None)
        if not key:
            raise RuntimeError(
                "TENANT_SECRETS_KEY no configurada. "
                "Generar con cryptography.fernet.Fernet.generate_key()"
            )
        self._fernet = Fernet(key.encode())

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        try:
            return self._fernet.decrypt(ciphertext.encode()).decode()
        except InvalidToken as exc:
            raise ValueError("DSN cifrado inválido o clave maestra incorrecta") from exc


_secret_provider: Optional[SecretProvider] = None


def get_secret_provider() -> SecretProvider:
    global _secret_provider
    if _secret_provider is None:
        _secret_provider = FernetSecretProvider()
    return _secret_provider


def save_tenant_connection(
    tenant_id: str,
    dsn: str,
    provider: str = "supabase_postgres",
    project_ref: Optional[str] = None,
    secret_ref: Optional[str] = None,
) -> None:
    """
    Cifra y persiste el DSN de un cliente en app_auth.tenant_connections.
    Uso exclusivo en scripts/admin de onboarding.
    """
    from auth.db import db_cursor
    encrypted = get_secret_provider().encrypt(dsn)
    with db_cursor(commit=True) as cur:
        cur.execute(
            """
            INSERT INTO app_auth.tenant_connections
                (tenant_id, provider, project_ref, encrypted_dsn, secret_ref, status, last_verified_at)
            VALUES (%s, %s, %s, %s, %s, 'active', NOW())
            ON CONFLICT (tenant_id) DO UPDATE SET
                provider = EXCLUDED.provider,
                project_ref = EXCLUDED.project_ref,
                encrypted_dsn = EXCLUDED.encrypted_dsn,
                secret_ref = EXCLUDED.secret_ref,
                status = 'active',
                last_verified_at = NOW(),
                updated_at = NOW()
            """,
            (tenant_id, provider, project_ref, encrypted, secret_ref),
        )
