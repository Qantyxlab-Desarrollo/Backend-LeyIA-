import asyncio, asyncpg

async def main():
    # Usa el rol postgres del proyecto central
    dsn = "postgresql://postgres:TU_PASSWORD_POSTGRES@db.lvxmydkeicvhbhxsjfoz.supabase.co:5432/postgres?sslmode=require"
    
    conn = await asyncpg.connect(dsn)
    print("Conectado a:", await conn.fetchrow("SELECT current_database(), inet_server_addr(), inet_server_port()"))
    
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS public.tenant_databases (
            tenant_id UUID PRIMARY KEY,
            tenant_data_dsn TEXT NOT NULL,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    
    await conn.execute("""
        INSERT INTO public.tenant_databases (tenant_id, tenant_data_dsn)
        VALUES (
            '5e828f41-aed8-44ed-adf8-519ee52f67d8',
            'postgresql://postgres:11092025Ma!Manzzo@db.vnjbztldcmkrsthjgrnd.supabase.co:5432/postgres?sslmode=require'
        )
        ON CONFLICT (tenant_id) DO UPDATE SET tenant_data_dsn = EXCLUDED.tenant_data_dsn
    """)
    
    rows = await conn.fetch("SELECT * FROM public.tenant_databases")
    print("Registros:", rows)
    await conn.close()

asyncio.run(main())
