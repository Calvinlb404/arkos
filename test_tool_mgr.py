import asyncio
import os
import sys

sys.path.append("/home/nmorgan/dev/arkos")
from dotenv import load_dotenv
load_dotenv("/home/nmorgan/dev/arkos/.env")

from base_module.app import tool_manager
import aiohttp

async def main():
    await tool_manager.initialize_servers()
    
    # We want to test user_id = "a565ec08-5aca-425f-93d6-a9dda21bd30a" (the one we saw was connected)
    user_id = "a565ec08-5aca-425f-93d6-a9dda21bd30a"
    svc_name = "linear"
    
    print(f"Testing _ensure_user_server for user_id={user_id}, svc_name={svc_name}")
    async with aiohttp.ClientSession() as sess:
        try:
            await tool_manager._ensure_user_server(sess, user_id, svc_name)
            print("SUCCESS! Tools in _user_tools:")
            tools = tool_manager._user_tools.get(user_id, {})
            print(list(tools.keys()))
        except Exception as e:
            import traceback
            print(f"FAILED: {type(e).__name__}: {e}")
            traceback.print_exc()

asyncio.run(main())
