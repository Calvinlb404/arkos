"""
Single chokepoint for parsing LLM structured output into Pydantic models.

All state files call parse_llm_json instead of model_validate_json directly so
repair logic lives in one place. json_repair handles the most common model
failure modes: truncated objects, trailing commas, code-fence wrappers, unquoted
keys. If repair still yields something unparseable, OutputValidationError is
raised with a model-actionable field-level summary — never the raw content.
"""

from __future__ import annotations

import logging
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from model_module.errors import OutputValidationError

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


def _field_summary(exc: Exception) -> str:
    """Condense a pydantic ValidationError into a short, model-actionable phrase.

    e.g. "final: Field required; route: Input should be 'reply' | 'ask' | ..."
    Falls back to str(exc)[:200] for non-ValidationError inputs.
    """
    errors_fn = getattr(exc, "errors", None)
    if callable(errors_fn):
        parts: list[str] = []
        for err in exc.errors():
            loc = ".".join(str(x) for x in err.get("loc", ())) or "(root)"
            parts.append(f"{loc}: {err.get('msg', 'invalid')}")
        if parts:
            return "; ".join(parts[:5])  # cap at 5 fields for readability
    return str(exc)[:200]


def parse_llm_json(content: str | None, model: type[T]) -> T:
    """
    Parse LLM output into a Pydantic model with automatic repair.

    Runs json_repair on the raw string before attempting validation so that
    common model failures (trailing commas, truncation, fence wrappers) are
    corrected before Pydantic sees the input.

    Raises OutputValidationError (not ValidationError) on all parse failures so
    callers have one exception type to handle and _run_state can classify it.
    The .detail attribute is model-actionable; .raw is the original content
    (logged here, never surfaced to the user).
    """
    if not content or not content.strip():
        raise OutputValidationError("model returned empty content")

    try:
        from json_repair import repair_json  # type: ignore[import]
        repaired: Any = repair_json(content, return_objects=False)
    except Exception:
        # json_repair unavailable or crashed — fall through to raw content
        repaired = content

    try:
        return model.model_validate_json(repaired if isinstance(repaired, str) else content)
    except (ValidationError, ValueError) as e:
        detail = _field_summary(e)
        logger.warning(
            "parse_llm_json failed for %s: %s (raw length=%d)",
            model.__name__,
            detail,
            len(content),
        )
        raise OutputValidationError(detail, raw=content) from e
