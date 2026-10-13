# scripts/generate_recovery_link.py
import os
import sys

import httpx
from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from auth.config import settings


def main():
    email = input("Email del usuario: ").strip().lower()

    service_role = settings.CENTRAL_SUPABASE_SERVICE_ROLE_KEY
    supabase_url = settings.CENTRAL_SUPABASE_URL.rstrip("/")

    r = httpx.post(
        f"{supabase_url}/auth/v1/admin/generate_link",
        headers={"apikey": service_role, "Authorization": f"Bearer {service_role}"},
        json={
            "type": "recovery",
            "email": email,
            "redirect_to": f"{settings.FRONTEND_URL}/definir-password",
        },
        timeout=15,
    )

    if r.status_code >= 400:
        print(f"❌ Error {r.status_code}: {r.text[:300]}")
        return

    data = r.json()
    print(f"\n⬇️  LINK PARA DEFINIR CONTRASEÑA:")
    print(f"   {data.get('action_link')}")
    print(f"\n   Enviar por WhatsApp/email a {email}")


if __name__ == "__main__":
    main()
