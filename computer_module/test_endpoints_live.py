"""
Live test of Tasks 8+9 HTTP endpoints:
  POST /computer/tasks     dispatch
  GET  /computer/tasks     list
  GET  /computer/tasks/:id  get
  GET  /computer/tasks/:id/events  poll
  GET  /computer/files     filesystem
  GET  /computer/file      read file
"""

import asyncio
import json

import httpx
from dotenv import load_dotenv

load_dotenv(".env")

# Get a demo token from /auth/demo-login
BASE = "http://localhost:1114"


async def main():
    async with httpx.AsyncClient(base_url=BASE, timeout=30) as client:
        # --- auth ---
        r = await client.post("/auth/demo-login", json={"username": "endpoint_test_user"})
        token = r.json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        print(f"token: {token[:40]}...")

        # --- dispatch ---
        print("\n--- POST /computer/tasks ---")
        r = await client.post(
            "/computer/tasks",
            json={"prompt": "write /home/user/api_test.py with: print('api endpoint works'), then run it"},
            headers=headers,
        )
        print(f"status: {r.status_code}")
        body = r.json()
        print(f"body: {json.dumps(body, indent=2)}")
        task_id = body.get("task_id")
        if not task_id:
            print("no task_id, aborting")
            return

        # --- wait for completion by polling ---
        print(f"\n--- polling /computer/tasks/{task_id}/events until done ---")
        last_id = 0
        completed = False
        for _ in range(60):  # poll up to 60s
            await asyncio.sleep(1)
            r = await client.get(f"/computer/tasks/{task_id}/events?after={last_id}", headers=headers)
            events = r.json().get("events", [])
            for e in events:
                last_id = e["event_id"]
                print(f"  [{e['kind']}] {e['content'][:70]}")
                if e["kind"] in ("completed", "failed"):
                    completed = True
                    break
            if completed:
                break

        # --- list tasks ---
        print("\n--- GET /computer/tasks ---")
        r = await client.get("/computer/tasks", headers=headers)
        tasks = r.json().get("tasks", [])
        print(f"found {len(tasks)} task(s), latest status: {tasks[0]['status'] if tasks else 'none'}")

        # --- filesystem ---
        print("\n--- GET /computer/files ---")
        r = await client.get("/computer/files?path=/home/user", headers=headers)
        entries = r.json().get("entries", [])
        print(f"entries: {[e['name'] for e in entries]}")

        # --- read the file the agent wrote ---
        print("\n--- GET /computer/file?path=/home/user/api_test.py ---")
        r = await client.get("/computer/file?path=/home/user/api_test.py", headers=headers)
        data = r.json()
        print(f"status: {r.status_code}")
        print(f"content: {repr(data.get('content', ''))[:100]}")
        print(f"truncated: {data.get('truncated')}, size: {data.get('size')}")


asyncio.run(main())
