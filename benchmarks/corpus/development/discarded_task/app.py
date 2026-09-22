import asyncio
async def worker(): pass
async def run():
    asyncio.create_task(worker())
