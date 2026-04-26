"""Quick test: upsert with JSON-only Accept, jsonrpc with SSE Accept."""
import asyncio
import json
import os
import sys

import aiohttp

sys.path.append("/home/nmorgan/dev/arkos")
from dotenv import load_dotenv

load_dotenv("/home/nmorgan/dev/arkos/.env")

API_KEY = os.environ.get("SMITHERY_API_KEY", "")
NAMESPACE = os.environ.get("SMITHERY_NAMESPACE", "arkos")
BASE = "https://api.smithery.ai"

def json_headers():
    return {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

def sse_headers():
    return {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }

async def main():
    conn_id = "test-debug-linear2"
    timeout = aiohttp.ClientTimeout(total=15)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        # Step 1: upsert with JSON-only Accept
        url1 = f"{BASE}/connect/{NAMESPACE}/{conn_id}"
        body1 = {"mcpUrl": "https://linear.run.tools", "name": "Linear"}
        print(f"PUT {url1}")
        async with session.put(url1, json=body1, headers=json_headers()) as resp:
            print(f"  status: {resp.status}")
            print(f"  content-type: {resp.headers.get('Content-Type')}")
            text = await resp.text()
            print(f"  body: {text[:500]}")
            data = json.loads(text) if text else {}
            status = data.get("status")
            print(f"  parsed status: {status}")

        # Step 2: tools/list with SSE Accept
        url2 = f"{BASE}/connect/{NAMESPACE}/{conn_id}/mcp"
        rpc = {"jsonrpc": "2.0", "id": "test1", "method": "tools/list", "params": {}}
        print(f"\nPOST {url2}")
        async with session.post(url2, json=rpc, headers=sse_headers()) as resp:
            print(f"  status: {resp.status}")
            ct = resp.headers.get("Content-Type", "")
            print(f"  content-type: {ct}")

            if "text/event-stream" in ct:
                print("  Reading SSE stream...")
                buf = b""
                try:
                    while True:
                        chunk = await asyncio.wait_for(resp.content.read(4096), timeout=10)
                        if not chunk:
                            print("  [EOF]")
                            break
                        buf += chunk
                        print(f"  [chunk {len(chunk)}b]")
                        # Check for complete JSON-RPC response in buffered lines
                        decoded = buf.decode("utf-8", errors="replace")
                        for line in decoded.split("\n"):
                            line = line.strip()
                            if line.startswith("data: "):
                                try:
                                    parsed = json.loads(line[6:])
                                    if "result" in parsed or "error" in parsed:
                                        print(f"  GOT RESULT! keys={list(parsed.keys())}")
                                        if "result" in parsed:
                                            tools = parsed["result"].get("tools", [])
                                            print(f"  {len(tools)} tools found")
                                            for t in tools[:3]:
                                                print(f"    - {t.get('name')}")
                                        raise StopAsyncIteration
                                except json.JSONDecodeError:
                                    pass
                except StopAsyncIteration:
                    print("  Done!")
                except TimeoutError:
                    print(f"  [TIMEOUT] buffered {len(buf)}b")
                    print(f"  raw: {buf[:500]}")
            else:
                text = await resp.text()
                print(f"  body: {text[:1000]}")

asyncio.run(main())
