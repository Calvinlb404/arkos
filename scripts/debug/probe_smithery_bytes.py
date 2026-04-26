import asyncio
import os
import sys

import aiohttp

sys.path.append("/home/nmorgan/dev/arkos")
from dotenv import load_dotenv

load_dotenv("/home/nmorgan/dev/arkos/.env")

API_KEY = os.environ.get("SMITHERY_API_KEY", "")
NAMESPACE = os.environ.get("SMITHERY_NAMESPACE", "arkos")
BASE = "https://api.smithery.ai"
USER_ID = "a565ec08-5aca-425f-93d6-a9dda21bd30a"
CONN_ID = f"user-{USER_ID}__linear"

async def test():
    async with aiohttp.ClientSession() as sess:
        headers = {
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream"
        }
        rpc = {"jsonrpc": "2.0", "id": "test_1", "method": "tools/list", "params": {}}
        url = f"{BASE}/connect/{NAMESPACE}/{CONN_ID}/mcp"
        
        print("POST", url, flush=True)
        async with sess.post(url, json=rpc, headers=headers) as resp:
            print("Status:", resp.status, flush=True)
            print("Content-Type:", resp.headers.get("Content-Type"), flush=True)
            
            print("Reading raw bytes...", flush=True)
            try:
                # Read single bytes to see exactly what comes in immediately!
                while True:
                    b = await asyncio.wait_for(resp.content.read(1), timeout=10)
                    if not b:
                        print("\nEOF")
                        break
                    sys.stdout.buffer.write(b)
                    sys.stdout.flush()
            except TimeoutError:
                print("\nTIMEOUT", flush=True)

asyncio.run(test())
