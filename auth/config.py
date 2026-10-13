from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Set


class AuthSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
    )

    # ── Base de datos CENTRAL de LeyIA ───────────────────────────────
    CENTRAL_DATABASE_URL: str = ""

    # ── Proyecto Supabase CENTRAL de autenticación ───────────────────
    CENTRAL_SUPABASE_URL: str = ""
    CENTRAL_SUPABASE_ANON_KEY: str = ""
    CENTRAL_SUPABASE_SERVICE_ROLE_KEY: str | None = None   # solo backend, sellada
    CENTRAL_SUPABASE_JWT_SECRET: str | None = None           # legacy HS256; vacío si RS256/ES256

    # ── Frontend y cookies ────────────────────────────────────────────
    FRONTEND_URL: str = "http://localhost:3000"
    API_URL: str = "http://localhost:8001"
    SECURE_COOKIES: bool = False
    COOKIE_DOMAIN: str | None = None
    ACCESS_TOKEN_COOKIE_NAME: str = "leyia_session"

    # ── Secretos de tenants (DSNs cifrados) ─────────────────────────
    TENANT_SECRETS_KEY: str | None = None                  # Fernet key, sellada

    # ── Staff LeyIA (emails separados por coma) ─────────────────────
    ADMIN_EMAILS: str = ""

    # ── Multi-tenant ────────────────────────────────────────────────
    ENVIRONMENT: str = "development"  # "development" | "production"
    DEV_TENANT_ID: str = ""          # VACÍO en producción

    @property
    def admin_email_set(self) -> Set[str]:
        return {e.strip().lower() for e in self.ADMIN_EMAILS.split(",") if e.strip()}

    @property
    def central_issuer(self) -> str:
        return self.CENTRAL_SUPABASE_URL.rstrip("/") + "/auth/v1"

    @property
    def dev_tenant_bypass_enabled(self) -> bool:
        return self.ENVIRONMENT != "production" and bool(self.DEV_TENANT_ID)


settings = AuthSettings()
