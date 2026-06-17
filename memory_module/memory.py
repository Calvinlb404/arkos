# memory.py
import os
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

from dotenv import load_dotenv
from mem0 import Memory as Mem0Memory
from psycopg2 import pool

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


from model_module.ArkModelNew import (
    AIMessage,
    Message,
    SystemMessage,
    ToolMessage,
    UserMessage,
)

ROLE_TO_CLASS: dict[str, type[Message]] = {
    "system": SystemMessage,
    "user": UserMessage,
    "assistant": AIMessage,
    "tool": ToolMessage,
}


CLASS_TO_ROLE: dict[type[Message], str] = {
    SystemMessage: "system",
    UserMessage: "user",
    AIMessage: "assistant",
    ToolMessage: "tool",
}


# Global Mem0 config ---------------------
# Load .env file
load_dotenv()
# (removed: os.environ["OPENAI_API_KEY"] = "sk" -- it overwrote the real key at
#  import time and silently broke mem0. MULTIUSER Task 3 / UNSAFE_DECISIONS.)

config = {
    "vector_store": {
        "provider": "supabase",
        "config": {
            "connection_string": os.environ["DB_URL"],
            "collection_name": "memories",
            "index_method": "hnsw",
            "index_measure": "cosine_distance",
        },
    },
    "llm": {
        "provider": "vllm",
        "config": {
            "model": "Qwen/Qwen2.5-7B-Instruct",
            "vllm_base_url": "http://localhost:30000/v1",
        },
    },
    "embedder": {
        "provider": "huggingface",
        "config": {"huggingface_base_url": "http://localhost:4444/v1"},
    },
}

# Global connection pool (initialized lazily)
_connection_pool = None
_pool_lock = threading.Lock()

# Background executor for non-blocking mem0 operations
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="mem0_bg")


def _get_pool(db_url: str):
    """Get or create the global connection pool."""
    global _connection_pool
    if _connection_pool is None:
        with _pool_lock:
            if _connection_pool is None:
                _connection_pool = pool.ThreadedConnectionPool(minconn=1, maxconn=10, dsn=db_url)
    return _connection_pool


class Memory:
    """
    Connects agent to supabase backend for long
    and short term memories

    """

    def __init__(self, user_id: str, session_id: str, db_url: str, use_long_term: bool = True):
        self.user_id = user_id
        self.db_url = db_url
        self.use_long_term = use_long_term  # Toggle for long-term memory

        # Initialize connection pool
        self._pool = _get_pool(db_url)

        # initialize mem0 (lazy - only if needed)
        self._mem0 = None
        if self.use_long_term:
            self._mem0 = Mem0Memory.from_config(config)

        # session handling
        self.session_id = session_id if session_id is not None else str(uuid.uuid4())

    def start_new_session(self):
        """Start a new chat session."""
        self.session_id = str(uuid.uuid4())
        return self.session_id

    def serialize(self, message: Message) -> str:
        """
        Convert a Message subclass into the string stored in Postgres.
        Store role separately in the role column.
        """
        return message.model_dump_json()

    def deserialize(self, message: str, role: str) -> Message:
        """
        Convert the stored Postgres string back into the correct Message subclass.
        Requires the role column value.
        """
        cls = ROLE_TO_CLASS.get(role)
        if cls is None:
            raise ValueError(f"Unknown role: {role}")
        return cls.model_validate_json(message)

    def _add_to_mem0_background(self, content: str, metadata: dict):
        """Background task to add to mem0 (non-blocking)."""
        try:
            if self._mem0:
                self._mem0.add(messages=content, metadata=metadata, user_id=self.user_id)
        except Exception as e:
            print(f"[mem0 background] Error: {e}")

    async def add_memory(self, message) -> bool:
        """Add a single turn to Postgres (fast) + Mem0 in background."""
        import asyncio

        try:
            role = CLASS_TO_ROLE[type(message)]
            serialized = self.serialize(message)
            user_id, session_id = self.user_id, self.session_id

            def _insert():
                conn = self._pool.getconn()
                try:
                    cur = conn.cursor()
                    cur.execute(
                        """
                        INSERT INTO conversation_context (user_id, session_id, role, message)
                        VALUES (%s, %s, %s, %s)
                        """,
                        (user_id, session_id, role, serialized),
                    )
                    conn.commit()
                    cur.close()
                finally:
                    self._pool.putconn(conn)

            await asyncio.to_thread(_insert)

            # Store in mem0 in background (non-blocking)
            if self.use_long_term and self._mem0 and message.content:
                metadata = {"user_id": user_id, "session_id": session_id, "role": role}
                _executor.submit(self._add_to_mem0_background, message.content, metadata)

            return True

        except Exception:
            import traceback

            traceback.print_exc()
            return False

    async def retrieve_long_memory(self, context: list | None = None, mem0_limit: int = 10) -> SystemMessage:
        """Retrieve relevant long term memories for the current user."""
        import asyncio

        if context is None:
            context = []
        if not self.use_long_term or not self._mem0:
            return SystemMessage(content="")

        try:
            query = " ".join(m.content for m in context[-2:] if hasattr(m, "content"))

            if not query.strip():
                return SystemMessage(content="")

            results = await asyncio.to_thread(self._mem0.search, query=query, user_id=self.user_id, limit=mem0_limit)

            memory_entries = [f"{r.get('role', 'user')}: {r['memory']}" for r in results.get("results", [])]

            if not memory_entries:
                return SystemMessage(content="")

            memory_string = "retrieved memories:\n" + "\n".join(memory_entries)
            return SystemMessage(content=memory_string)

        except Exception as e:
            print(f"[retrieve_long_memory] Error: {e}")
            return SystemMessage(content="")

    async def retrieve_short_memory(self, turns):
        """Retrieve relevant short term memories for the current user"""
        import asyncio

        try:

            def _fetch():
                conn = self._pool.getconn()
                try:
                    cur = conn.cursor()
                    # Scope to (user_id, session_id). Without the session filter,
                    # this returned the user's last N turns across ALL their
                    # conversations, so unrelated chats bled into each other (Fix 5).
                    cur.execute(
                        """
                        SELECT role, message
                        FROM (
                            SELECT id, role, message
                            FROM conversation_context
                            WHERE user_id = %s AND session_id = %s
                            ORDER BY id DESC
                            LIMIT %s
                        ) sub
                        ORDER BY id ASC
                        """,
                        (self.user_id, self.session_id, turns),
                    )
                    rows = cur.fetchall()
                    cur.close()
                    return rows
                finally:
                    self._pool.putconn(conn)

            rows = await asyncio.to_thread(_fetch)
            return [self.deserialize(message=msg, role=role) for role, msg in rows]

        except Exception as e:
            print(f"[retrieve_short_memory] Error: {e}")
            return []


if __name__ == "__main__":
    test_instance = Memory(user_id="alice_test", session_id="session_test", db_url=os.environ["DB_URL"])

    print(test_instance.add_memory(SystemMessage(content="My favorite color is blue and I live in New York")))

    context = test_instance.retrieve_short_memory(turns=2)
    print(context)

    print(test_instance.retrieve_long_memory(context))
