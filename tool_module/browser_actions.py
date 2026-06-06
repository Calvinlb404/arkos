"""
Custom `browser_use.Tools` actions registered alongside the built-in agent
toolkit. Each one collapses a multi-step LLM-reasoned flow into a single
deterministic tool call.

Lazy imports throughout: arkos must still boot on hosts where browser_use
isn't installed (tests mock it via sys.modules). `build_arkos_tools()`
returns None in that environment and the caller falls back to passing
nothing for `tools=`.

Design notes:
- Actions are async because the cdp_use client is async-only.
- We talk to Chromium via `browser_session.get_or_create_cdp_session(...)` so
  we share the agent's existing CDP socket; opening a second connection to
  Browserless would land us on a different browser instance entirely.
- Heuristic JS evaluation is preferred over per-site selectors: the goal is
  graceful degradation across the long tail of consent UIs, not perfection
  on any one site.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


# JavaScript heuristic for dismissing consent/cookie/age-gate overlays.
# Strategy: walk every visible button / role=button / clickable anchor on the
# page, score it by how close its accessible text matches common consent
# verbs in several languages, click the highest-scoring match if any clears
# a confidence threshold. Returns the chosen label and total candidates
# considered so the LLM gets a useful breadcrumb in the action result.
_DISMISS_OVERLAY_JS = r"""
(() => {
  const TERMS = [
    // English
    'accept all', 'accept cookies', 'accept', 'agree', 'i agree',
    'allow all', 'allow', 'got it', 'ok', 'continue', 'consent',
    'dismiss', 'close', 'reject all', 'decline', 'no thanks',
    // Common non-English
    'aceptar', 'aceptar todo', 'acepto',
    'einverstanden', 'akzeptieren', 'zustimmen',
    'accepter', 'accepter tout', "j'accepte",
    'concordo', 'aceitar',
  ];
  const visible = (el) => {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    if (r.width < 4 || r.height < 4) return false;
    const s = getComputedStyle(el);
    return s.visibility !== 'hidden' && s.display !== 'none' && parseFloat(s.opacity) > 0.05;
  };
  const text = (el) => (el.innerText || el.textContent || el.getAttribute('aria-label') || el.value || '')
    .trim().toLowerCase().replace(/\s+/g, ' ');
  const score = (t) => {
    if (!t || t.length > 60) return 0;
    for (let i = 0; i < TERMS.length; i++) {
      const term = TERMS[i];
      if (t === term) return 100 - i;
      if (t.startsWith(term) || t.endsWith(term)) return 80 - i;
      if (t.includes(term)) return 50 - i;
    }
    return 0;
  };
  const sel = 'button, a, [role="button"], input[type="button"], input[type="submit"]';
  const elements = Array.from(document.querySelectorAll(sel)).filter(visible);
  let best = null;
  let bestScore = 0;
  for (const el of elements) {
    const s = score(text(el));
    if (s > bestScore) { best = el; bestScore = s; }
  }
  if (best && bestScore >= 50) {
    const label = text(best);
    best.click();
    return { clicked: true, label, score: bestScore, considered: elements.length };
  }
  return { clicked: false, considered: elements.length };
})()
"""


def _make_dismiss_overlay_action(tools_module: Any, action_result_cls: Any):
    """Closure factory so the decorator captures the real Tools instance."""

    @tools_module.action(
        description=(
            "Dismiss any cookie/consent/age-gate banner currently covering "
            "the page. Idempotent: a no-op if no banner is detected. Run "
            "this once on first load and again only if a new overlay "
            "appears mid-task."
        )
    )
    async def dismiss_overlay(browser_session: Any) -> Any:
        try:
            cdp_session = await browser_session.get_or_create_cdp_session(target_id=None, focus=False)
            result = await cdp_session.cdp_client.send.Runtime.evaluate(
                params={
                    "expression": _DISMISS_OVERLAY_JS,
                    "returnByValue": True,
                    "awaitPromise": False,
                },
                session_id=cdp_session.session_id,
            )
        except Exception as e:
            logger.info("dismiss_overlay: CDP evaluate failed: %s", e)
            return action_result_cls(
                extracted_content="dismiss_overlay: no overlay dismissed (page not ready)",
                include_in_memory=False,
            )

        value = ((result or {}).get("result") or {}).get("value") or {}
        if value.get("clicked"):
            return action_result_cls(
                extracted_content=(
                    f"Dismissed overlay via element labelled {value.get('label')!r} "
                    f"(score={value.get('score')}, considered={value.get('considered')})"
                ),
                include_in_memory=True,
            )
        return action_result_cls(
            extracted_content=(
                f"No consent/cookie overlay detected on this page "
                f"(considered {value.get('considered', 0)} clickable elements)."
            ),
            include_in_memory=False,
        )

    return dismiss_overlay


def build_arkos_tools() -> Any | None:
    """Build a `browser_use.Tools` instance carrying arkos's custom actions.

    Returns None if browser_use isn't importable (lets the caller pass
    nothing for `tools=` without branching).
    """
    try:
        import browser_use as bu
    except ImportError:
        return None

    tools_cls = getattr(bu, "Tools", None) or getattr(bu, "Controller", None)
    action_result_cls = getattr(bu, "ActionResult", None)
    if tools_cls is None or action_result_cls is None:
        logger.info("browser_actions: Tools/ActionResult not exported by browser_use; skipping registration")
        return None

    try:
        tools = tools_cls()
    except Exception:
        logger.exception("browser_actions: failed to construct Tools()")
        return None

    try:
        _make_dismiss_overlay_action(tools, action_result_cls)
    except Exception:
        logger.exception("browser_actions: failed to register dismiss_overlay action")

    return tools
