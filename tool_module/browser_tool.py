"""
Browser automation tool: hand a natural-language task to a sandboxed Chromium
session managed by Browserless and return the final string result.

User isolation is delegated to Browserless: each call gets a fresh session
from the pool, torn down on completion or timeout. This module owns the
glue between Arkos's tool registry and the `browser-use` Agent, plus a
best-effort CDP screencast that forwards JPEG frames into the in-process
frame broker so a frontend pane can show the user what the agent is doing.

Configuration (env):
  BROWSERLESS_URL    CDP WebSocket URL, e.g. ws://browserless:3000 (required)
  SGLANG_URL         base URL of the in-cluster SGLang Qwen server
                     (default http://sglang:30000); the tool talks to its
                     OpenAI-compatible /v1 endpoint
  BROWSER_USE_MODEL  model name to send to SGLang (default "tgi", which the
                     SGLang launcher accepts as an alias for whatever model
                     is loaded)
  OPENAI_API_KEY     forwarded as the bearer token; SGLang ignores it but the
                     OpenAI client requires something. Defaults to "sk-dummy".
  BROWSER_STREAM_ENABLED  "0" to disable the screencast entirely (default on).
  BROWSER_USE_MAX_STEPS    hard cap on browser-use agent steps (default 25).
  BROWSER_USE_MAX_SECONDS  wall-clock timeout for one task in seconds
                           (default 180). Beats infinite reCAPTCHA loops.
  BROWSER_USE_MAX_FAILURES         consecutive failed steps before the agent
                                   gives up (default 3, matches browser-use).
  BROWSER_USE_MAX_ACTIONS_PER_STEP max actions browser-use bundles into one
                                   step, e.g. for filling multi-field forms
                                   (default 4, matches browser-use).
  BROWSER_USE_LLM_TIMEOUT          per-LLM-call timeout in seconds (default
                                   90, matches browser-use).
  BROWSER_USE_STEALTH              "1" to ask Browserless to launch the
                                   sandbox in stealth mode (puppeteer-extra-
                                   plugin-stealth — helps reduce reCAPTCHA
                                   detection). Default "1". The flag is
                                   appended to the CDP URL as ?stealth=true
                                   and is a no-op against non-Browserless
                                   CDP endpoints.
  BROWSER_USE_VISION               "1" to send screenshots to the LLM on
                                   every step. Default "0" because arkos's
                                   default SGLang model (Qwen/Qwen3-8B) is
                                   text-only — passing images burns prompt
                                   tokens and the model can't see them.
                                   Flip to "1" once SGLang serves a VL
                                   variant (e.g. Qwen3-VL).
  BROWSER_USE_THINKING             "1" to let browser-use inject its
                                   <think> scaffold into prompts. Default
                                   "0" — browser-use's scaffold isn't
                                   trained into stock Qwen-Instruct and
                                   adds latency without observable wins.
  BROWSER_USE_USE_JUDGE            "1" to run browser-use's built-in
                                   output judge after a task completes.
                                   Default "0" — the judge defaults to the
                                   main LLM and only pays off when a
                                   ground_truth is supplied, which arkos
                                   doesn't have.
  BROWSER_USE_ENABLE_PLANNING      "1" to let the Agent insert higher-level
                                   planning passes (default "1" matches
                                   browser-use 0.12).
  BROWSER_USE_REPLAN_ON_STALL      consecutive failed step count that
                                   triggers a planning replan (default 3).
  BROWSER_USE_EXPLORATION_LIMIT    steps between planning passes during
                                   normal progress (default 5).
  BROWSER_USE_FLASH_MODE           "1" to enable browser-use's flash mode,
                                   which trims the per-step prompt and skips
                                   some judgment passes for faster, lighter
                                   automation on simple tasks (default "0",
                                   the safer choice for diverse tasks).
  BROWSER_USE_INCLUDE_RECENT_EVENTS  "1" to surface recent browser-side
                                   events (network errors, navigation
                                   commits, alert dialogs) to the agent
                                   each step. Helps recovery from page-load
                                   races. Default "0" — matches browser-use.
  BROWSER_USE_EXTRA_GUIDANCE        extra free-form text appended to
                                   browser-use's built-in system prompt
                                   for every step. If unset, arkos appends
                                   its own defaults (be concise, dismiss
                                   cookie banners, never ask the user
                                   mid-task, prefer direct URLs over
                                   search-engine indirection).
  BROWSER_USE_MESSAGE_COMPACTION    "1" to let browser-use compact the
                                   message history once it grows past a
                                   threshold (default "1" — required for
                                   any task longer than ~10 steps to
                                   avoid blowing the model's context).
                                   Flip to "0" only to debug a step.
  BROWSER_USE_COMPACT_EVERY_N_STEPS  override browser-use's default
                                   compact_every_n_steps=25. Defaults to
                                   10 so compaction actually fires before
                                   BROWSER_USE_MAX_STEPS (default 25)
                                   cuts the run off. Set to 0 to fall
                                   back to the browser-use default.
  BROWSER_USE_LOOP_DETECTION        "1" to enable browser-use's loop
                                   detector (default "1"). Disabling it
                                   on purposefully-repetitive tasks (e.g.
                                   pagination scrapes) avoids false
                                   positives.
  BROWSER_USE_LOOP_WINDOW           number of trailing steps inspected
                                   for repetition (default 20).
  BROWSER_USE_MAX_HISTORY_ITEMS     hard cap on how many past steps are
                                   kept in the agent's working context
                                   after compaction. Unset = unlimited
                                   (browser-use default).
  BROWSER_USE_ALLOWED_DOMAINS       comma-separated list of domain
                                   patterns the agent is allowed to
                                   navigate to. Supports glob style
                                   ("https://*.example.com"). Empty
                                   (default) = unrestricted. Off-list
                                   navigations are rejected by the
                                   BrowserSession before they reach the
                                   network.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from typing import Any

from tool_module.browser_actions import build_arkos_tools
from tool_module.browser_stream import broker as _stream_broker

logger = logging.getLogger(__name__)


class BrowserToolError(RuntimeError):
    """Raised when the browser automation tool cannot run a task."""


def _stream_enabled() -> bool:
    return os.environ.get("BROWSER_STREAM_ENABLED", "1") != "0"


def _bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


# Default per-step guidance appended to browser-use's built-in system prompt.
# Tuned for arkos's typical task shape: a chat user asks buddy to do one
# concrete thing, we have no human in the loop during execution, and we want
# determinism over creativity. Override entirely via BROWSER_USE_EXTRA_GUIDANCE.
_DEFAULT_EXTRA_GUIDANCE = (
    "Operating context: you are arkos's automation agent running inside a "
    "headless Chromium sandbox. The user is NOT watching this execution and "
    "CANNOT answer questions mid-task — never wait for human input; if a "
    "step is ambiguous, make the most reasonable assumption and continue.\n"
    "\n"
    "Behaviour rules:\n"
    "  - Be concise. Return the smallest answer that satisfies the task.\n"
    "  - If the task names a specific URL, go directly to it; don't route "
    "through a search engine.\n"
    "  - When a cookie/consent banner blocks the page, dismiss it (accept or "
    "decline — whichever is one click away) and continue.\n"
    "  - When a modal/overlay covers the content you need, close it before "
    "trying to read or click underneath.\n"
    "  - Treat reCAPTCHA, login walls, and paywalls as task-blocking; report "
    "what you saw and stop rather than thrash on them.\n"
    "  - Never invent data. If a value isn't present on the page, say so.\n"
)


def _extra_guidance() -> str:
    return os.environ.get("BROWSER_USE_EXTRA_GUIDANCE") or _DEFAULT_EXTRA_GUIDANCE


def _build_compaction_settings() -> Any:
    """Decide what to pass for the Agent's `message_compaction` kwarg.

    browser-use 0.12.6's default compact_every_n_steps=25 is exactly our
    BROWSER_USE_MAX_STEPS cap, so compaction is effectively never reached on
    arkos — long tasks silently overflow the model's context window. Default
    to 10 here so compaction fires twice within a 25-step run and the
    context stays bounded.

    Returns:
      - False if BROWSER_USE_MESSAGE_COMPACTION=0  (compaction disabled)
      - True  if BROWSER_USE_COMPACT_EVERY_N_STEPS=0  (browser-use defaults)
      - a MessageCompactionSettings instance otherwise
    """
    if not _bool_env("BROWSER_USE_MESSAGE_COMPACTION", default=True):
        return False
    every_n = int(os.environ.get("BROWSER_USE_COMPACT_EVERY_N_STEPS", "10"))
    if every_n <= 0:
        return True
    try:
        from browser_use.agent.views import MessageCompactionSettings
    except ImportError:
        try:
            from browser_use import MessageCompactionSettings  # type: ignore
        except ImportError:
            # Older browser_use without the settings type — fall back to the
            # plain True bool; arkos's compaction will use library defaults.
            logger.info("browser_tool: MessageCompactionSettings not importable; using boolean compaction")
            return True
    try:
        return MessageCompactionSettings(compact_every_n_steps=every_n)
    except Exception:
        logger.exception("browser_tool: failed to instantiate MessageCompactionSettings; falling back to True")
        return True


async def _wait_for_agent_target(agent: Any, timeout_s: float = 10.0) -> bool:
    """Wait until browser-use has focused on a real Chromium target.

    browser-use 0.12 exposes the currently focused page's target id as
    `agent.browser_session.agent_focus_target_id`. Until that's populated,
    there's nothing to screencast.
    """
    deadline_loops = int(timeout_s / 0.1)
    for _ in range(deadline_loops):
        try:
            target_id = agent.browser_session.agent_focus_target_id
        except AttributeError:
            return False
        if target_id:
            return True
        await asyncio.sleep(0.1)
    return False


async def _run_screencast(agent: Any, user_id: str) -> None:
    """Stream CDP screencast frames from the agent's focused page to the broker.

    Uses the browser-use 0.12 CDP surface (cdp_use under the hood):
    `agent.browser_session.cdp_client` for event registration plus
    `agent.browser_session.get_or_create_cdp_session(target_id=None, focus=False)`
    for the per-target session id we send `Page.startScreencast` against.

    Any failure logs and exits quietly; the agent's own run is never affected.
    Cancelled by `run_browser_task`'s finally block.
    """
    if not await _wait_for_agent_target(agent):
        logger.info("browser_tool: no agent target within timeout; skipping screencast")
        return

    try:
        session = agent.browser_session
        cdp_session = await session.get_or_create_cdp_session(target_id=None, focus=False)
    except Exception:
        logger.exception("browser_tool: failed to acquire CDP session for screencast")
        return

    target_session_id = cdp_session.session_id

    def _on_frame(event: dict[str, Any], session_id: Any = None) -> None:
        # Only forward frames for OUR session; the shared cdp_client also fires
        # for other targets the agent attaches to during a run.
        if session_id is not None and session_id != target_session_id:
            return
        data = event.get("data")
        frame_session_id = event.get("sessionId")
        if data:
            _stream_broker.push_frame(user_id, data)
        if frame_session_id is not None:
            asyncio.create_task(_safe_ack(cdp_session, frame_session_id))

    try:
        session.cdp_client.register.Page.screencastFrame(_on_frame)
    except Exception:
        logger.exception("browser_tool: failed to register Page.screencastFrame handler")
        return

    try:
        await cdp_session.cdp_client.send.Page.startScreencast(
            params={
                "format": "jpeg",
                "quality": 60,
                "maxWidth": 1024,
                "maxHeight": 768,
                "everyNthFrame": 1,
            },
            session_id=target_session_id,
        )
    except Exception:
        logger.exception("browser_tool: Page.startScreencast failed")
        return

    try:
        # Stay alive until cancelled by run_browser_task's finally block.
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        with contextlib.suppress(Exception):
            await cdp_session.cdp_client.send.Page.stopScreencast(params={}, session_id=target_session_id)
        raise


async def _safe_ack(cdp_session: Any, session_id: int) -> None:
    with contextlib.suppress(Exception):
        await cdp_session.cdp_client.send.Page.screencastFrameAck(
            params={"sessionId": session_id},
            session_id=cdp_session.session_id,
        )


def _augment_cdp_url(url: str) -> str:
    """Append `stealth=true` to a Browserless CDP URL when stealth is enabled.

    Browserless reads query params from the WS handshake; `?stealth=true`
    launches the session via puppeteer-extra-plugin-stealth, which masks the
    automation signals that Google/Cloudflare/etc detect to trigger reCAPTCHA.

    No-op against any CDP endpoint that ignores the query string.
    """
    if os.environ.get("BROWSER_USE_STEALTH", "1") == "0":
        return url
    from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

    parts = urlparse(url)
    query = dict(parse_qsl(parts.query))
    if query.get("stealth") == "true":
        return url
    query["stealth"] = "true"
    return urlunparse(parts._replace(query=urlencode(query)))


def _parse_allowed_domains(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [s.strip() for s in raw.split(",") if s.strip()]


def _build_browser(browser_cls: Any, kwargs: dict[str, Any]) -> Any:
    """Construct a browser-use Browser, dropping kwargs the installed version
    doesn't accept. Same shape as _build_agent but for the Browser side."""
    import inspect

    try:
        sig = inspect.signature(browser_cls.__init__)
        params = sig.parameters
    except (TypeError, ValueError):
        return browser_cls(**kwargs)

    accepts_kwargs = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
    accepted: dict[str, Any] = {}
    dropped: list[str] = []
    for k, v in kwargs.items():
        if k == "cdp_url" or k in params or accepts_kwargs:
            accepted[k] = v
        else:
            dropped.append(k)
    if dropped:
        logger.info("browser_tool: Browser does not accept %s; dropping", dropped)
    return browser_cls(**accepted)


_REQUIRED_AGENT_KWARGS = frozenset({"task", "llm", "browser"})


def _build_agent(agent_cls: Any, kwargs: dict[str, Any]) -> Any:
    """Construct a browser-use Agent, dropping optional kwargs the installed
    version doesn't accept.

    browser-use moves parameters in and out of the Agent constructor across
    minor releases. We introspect the signature and pass only what's
    accepted — required kwargs always go through; optional ones are dropped
    silently. Beats version-pinning the kwarg list against a fast-moving dep.
    """
    import inspect

    try:
        sig = inspect.signature(agent_cls.__init__)
        params = sig.parameters
    except (TypeError, ValueError):
        # Can't introspect (e.g. C-implemented class). Try as-is and let the
        # caller deal with any TypeError.
        return agent_cls(**kwargs)

    accepts_kwargs = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
    accepted: dict[str, Any] = {}
    dropped: list[str] = []
    for k, v in kwargs.items():
        if k in _REQUIRED_AGENT_KWARGS or k in params or accepts_kwargs:
            accepted[k] = v
        else:
            dropped.append(k)
    if dropped:
        logger.info("browser_tool: Agent does not accept %s; dropping", dropped)
    return agent_cls(**accepted)


async def run_browser_task(user_id: str, task: str) -> str:
    """Run a single browser task in an isolated Browserless session.

    Returns the agent's final string result. Raises BrowserToolError if
    Browserless is unreachable or the configured CDP endpoint is missing.
    """
    cdp_url = os.environ.get("BROWSERLESS_URL")
    if not cdp_url:
        raise BrowserToolError("BROWSERLESS_URL is not set; cannot reach the sandboxed browser pool")

    # Lazy imports so the rest of arkos can boot without browser-use installed.
    try:
        from browser_use import Agent, Browser, ChatOpenAI
    except ImportError as e:
        raise BrowserToolError(f"browser-use is not installed in this environment: {e}") from e

    sglang_base = os.environ.get("SGLANG_URL", "http://sglang:30000").rstrip("/")
    llm_base_url = f"{sglang_base}/v1"
    llm_model = os.environ.get("BROWSER_USE_MODEL", "tgi")
    llm_api_key = os.environ.get("OPENAI_API_KEY", "sk-dummy")

    logger.info(
        "browser_tool: user=%s cdp=%s llm=%s model=%s",
        user_id,
        cdp_url,
        llm_base_url,
        llm_model,
    )
    max_steps = int(os.environ.get("BROWSER_USE_MAX_STEPS", "25"))
    max_seconds = float(os.environ.get("BROWSER_USE_MAX_SECONDS", "180"))
    max_failures = int(os.environ.get("BROWSER_USE_MAX_FAILURES", "3"))
    max_actions_per_step = int(os.environ.get("BROWSER_USE_MAX_ACTIONS_PER_STEP", "4"))
    llm_timeout = int(os.environ.get("BROWSER_USE_LLM_TIMEOUT", "90"))
    use_vision = _bool_env("BROWSER_USE_VISION", default=False)
    use_thinking = _bool_env("BROWSER_USE_THINKING", default=False)
    use_judge = _bool_env("BROWSER_USE_USE_JUDGE", default=False)
    enable_planning = _bool_env("BROWSER_USE_ENABLE_PLANNING", default=True)
    replan_on_stall = int(os.environ.get("BROWSER_USE_REPLAN_ON_STALL", "3"))
    exploration_limit = int(os.environ.get("BROWSER_USE_EXPLORATION_LIMIT", "5"))
    flash_mode = _bool_env("BROWSER_USE_FLASH_MODE", default=False)
    include_recent_events = _bool_env("BROWSER_USE_INCLUDE_RECENT_EVENTS", default=False)
    extra_guidance = _extra_guidance()
    message_compaction = _build_compaction_settings()
    loop_detection_enabled = _bool_env("BROWSER_USE_LOOP_DETECTION", default=True)
    loop_detection_window = int(os.environ.get("BROWSER_USE_LOOP_WINDOW", "20"))
    max_history_items_raw = os.environ.get("BROWSER_USE_MAX_HISTORY_ITEMS")
    max_history_items = int(max_history_items_raw) if max_history_items_raw else None

    effective_cdp_url = _augment_cdp_url(cdp_url)
    allowed_domains = _parse_allowed_domains(os.environ.get("BROWSER_USE_ALLOWED_DOMAINS"))
    browser_kwargs: dict[str, Any] = {"cdp_url": effective_cdp_url, "is_local": False}
    if allowed_domains:
        browser_kwargs["allowed_domains"] = allowed_domains
    browser = _build_browser(Browser, browser_kwargs)

    arkos_tools = build_arkos_tools() if _bool_env("BROWSER_USE_CUSTOM_TOOLS", default=True) else None
    agent_kwargs: dict[str, Any] = {
        "task": task,
        "llm": ChatOpenAI(model=llm_model, base_url=llm_base_url, api_key=llm_api_key),
        "browser": browser,
        "max_failures": max_failures,
        "max_actions_per_step": max_actions_per_step,
        "llm_timeout": llm_timeout,
        "use_vision": use_vision,
        "use_thinking": use_thinking,
        "use_judge": use_judge,
        "enable_planning": enable_planning,
        "planning_replan_on_stall": replan_on_stall,
        "planning_exploration_limit": exploration_limit,
        "flash_mode": flash_mode,
        "include_recent_events": include_recent_events,
        "extend_system_message": extra_guidance,
        "message_compaction": message_compaction,
        "loop_detection_enabled": loop_detection_enabled,
        "loop_detection_window": loop_detection_window,
        "max_history_items": max_history_items,
    }
    if arkos_tools is not None:
        agent_kwargs["tools"] = arkos_tools
    agent = _build_agent(Agent, agent_kwargs)

    screencast_task: asyncio.Task[None] | None = None
    if _stream_enabled():
        _stream_broker.start_session(user_id)
        screencast_task = asyncio.create_task(_run_screencast(agent, user_id))

    async def _run_with_step_cap():
        try:
            return await agent.run(max_steps=max_steps)
        except TypeError:
            # Older/newer browser-use versions may not accept max_steps; fall
            # back to the wall-clock timeout alone.
            return await agent.run()

    history = None
    try:
        history = await asyncio.wait_for(_run_with_step_cap(), timeout=max_seconds)
    except TimeoutError as e:
        raise BrowserToolError(f"browser task exceeded the {max_seconds:.0f}s wall-clock limit; aborting") from e
    except Exception as e:
        raise BrowserToolError(f"browser task failed: {e}") from e
    finally:
        if screencast_task is not None:
            screencast_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await screencast_task
            _stream_broker.end_session(user_id)
        try:
            await browser.close()
        except Exception:
            logger.exception("browser_tool: error closing browser session")

    final = history.final_result() if hasattr(history, "final_result") else str(history)
    return final or ""


async def _handler(arguments: dict[str, Any], user_id: str | None) -> dict[str, Any]:
    task = arguments.get("task")
    if not task or not isinstance(task, str):
        raise BrowserToolError("browser_task requires a non-empty 'task' string argument")
    result = await run_browser_task(user_id or "anonymous", task)
    return {"content": [{"type": "text", "text": result}]}


def register_browser_tool(tool_manager: Any) -> None:
    """Register `browser_task` as a local tool on the shared tool manager.

    Safe to call when tool_manager is None (e.g. Smithery disabled in dev) — it
    becomes a no-op so app startup still succeeds.
    """
    if tool_manager is None:
        logger.info("browser_tool: tool_manager is None; skipping registration")
        return
    tool_manager.register_local_tool(
        name="browser_task",
        description=(
            "Run a natural-language browser automation task in a sandboxed "
            "Chromium session. Pass a 'task' string describing what the "
            "browser should do; receive the final result text."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "Natural-language description of the browser task",
                },
            },
            "required": ["task"],
        },
        handler=_handler,
    )
    logger.info("browser_tool: registered browser_task on tool_manager")
