import asyncio, asyncpg, os
from dotenv import load_dotenv
load_dotenv()
async def main():
    for name in ['DATABASE_URL', 'CENTRAL_DATABASE_URL']:
        dsn = os.getenv(name)
        if not dsn:
            print(f"{name}: no definida")
            continue
        try:
            conn = await asyncpg.connect(dsn)
            db = await conn.fetchval('SELECT current_database()')
            host = await conn.fetchval('SELECT inet_server_addr()')
            port = await conn.fetchval('SELECT inet_server_port()')
            n = await conn.fetchval('SELECT COUNT(*) FROM public.bookings')
            print(f"{name}: db={db} host={host}:{port} bookings={n}")
            await conn.close()
        except Exception as e:
            print(f"{name}: ERROR {e}")
asyncio.run(main())