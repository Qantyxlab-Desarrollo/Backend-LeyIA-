from typing import Optional
from pydantic import BaseModel


class User(BaseModel):
    id: str
    email: str
    full_name: Optional[str] = None
    role: str = "user"          # rol de plataforma: admin | user
    is_active: bool = True
    email_verified: bool = False


class TokenBody(BaseModel):
    access_token: str
