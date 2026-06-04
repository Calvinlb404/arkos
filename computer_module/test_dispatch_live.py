"""
Live end-to-end test of Tasks 6+7: dispatch → run → chat injection.
Run: PYTHONPATH=. python computer_module/test_dispatch_live.py
"""
import asyncio, time, uuid
from dotenv import load_dotenv
load_dotenv(".env")

from computer_module.store import create_computer_task, get_computer_task, list_computer_events
from computer_module.runner import run_computer_task
from computer_module.sandbox import sandbox_manager
from config_module.loader import config

USER = "dispatch_test_user"
CHAT_SESSION_ID = str(uuid.uuid4())

async def main():
    print(f"user_id       = {USER}")
    print(f"chat_session  = {CHAT_SESSION_ID}")

    # Create the task row (as dispatch state would)
    task_id = create_computer_task(USER, CHAT_SESSION_ID, "write /home/user/hello.py containing print('hello world'), run it, verify the output")
    print(f"task_id       = {task_id}")
    print(f"\nRunning computer task...")

    t0 = time.time()
    await run_computer_task(
        task_id=task_id,
        user_id=USER,
        chat_session_id=CHAT_SESSION_ID,
        prompt="write /home/user/hello.py containing print('hello world'), run it, verify the output",
    )
    elapsed = time.time() - t0

    # Read back the task row
    task = get_computer_task(task_id, USER)
    print(f"\n{'='*60}")
    print(f"STATUS  : {task['status']}")
    print(f"ELAPSED : {elapsed:.1f}s")
    print(f"SUMMARY : {task['summary']}")
    print(f"OUTPUTS : {task['outputs']}")

    # Show events
    events = list_computer_events(task_id, USER)
    print(f"\nEVENTS ({len(events)}):")
    for e in events:
        print(f"  [{e['kind']}] {e['content'][:80]}")

    # Check the chat injection landed in conversation_context
    import psycopg2, psycopg2.extras
    conn = psycopg2.connect(config.get("database.url"))
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT role, message FROM conversation_context WHERE user_id=%s AND session_id=%s ORDER BY id",
                (USER, CHAT_SESSION_ID)
            )
            rows = cur.fetchall()
        print(f"\nCHAT INJECTION ({len(rows)} rows in conversation_context):")
        for r in rows:
            import json
            msg = json.loads(r['message'])
            print(f"  [{r['role']}] {str(msg.get('content',''))[:120]}")
    finally:
        conn.close()

asyncio.run(main())
