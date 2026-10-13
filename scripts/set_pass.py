#!/usr/bin/env python3
import getpass
import os

import httpx
from dotenv import load_dotenv

load_dotenv()

supabase_url = os.getenv("CENTRAL_SUPABASE_URL", "").rstrip("/")
service_role = os.getenv("CENTRAL_SUPABASE_SERVICE_ROLE_KEY")

if not supabase_url or not service_role:
    raise SystemExit("❌ Faltan CENTRAL_SUPABASE_URL o CENTRAL_SUPABASE_SERVICE_ROLE_KEY")

email = input("Email del usuario: ").strip().lower()
password = getpass.getpass("Nueva contraseña (mínimo 10 caracteres): ").strip()

if len(password) < 10:
    raise SystemExit("❌ La contraseña debe tener al menos 10 caracteres")

# Buscar usuario
r1 = httpx.get(
    f"{supabase_url}/auth/v1/admin/users",
    headers={"apikey": service_role, "Authorization": f"Bearer {service_role}"},
    params={"page": "1", "perPage": "100"},
    timeout=15,
)
r1.raise_for_status()

user_id = None
for u in r1.json().get("users", []):
    if (u.get("email") or "").lower() == email:
        user_id = u["id"]
        break

if not user_id:
    raise SystemExit(f"❌ Usuario {email} no encontrado")

# Actualizar contraseña
r2 = httpx.put(
    f"{supabase_url}/auth/v1/admin/users/{user_id}",
    headers={"apikey": service_role, "Authorization": f"Bearer {service_role}"},
    json={"password": password},
    timeout=15,
)

if r2.status_code >= 400:
    raise SystemExit(f"❌ Error {r2.status_code}: {r2.text[:300]}")

print(f"✅ Contraseña actualizada para {email}")
print("   Envíala por un canal seguro y pídele que la cambie al ingresar.")
