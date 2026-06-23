"""
Single chokepoint for parsing LLM structured output into Pydantic models.

All state files call parse_llm_json instead of model_validate_json directly so
repair logic lives in one place. json_repair handles the most common model
failure modes: truncated objects, trailing commas, code-fence wrappers, unquoted
keys. If repair still yields something unparseable, OutputValidationError is
raised with a model-actionable field-level summary — never the raw content.
"""

from __future__ import annotations

import json
import logging
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from model_module.errors import OutputValidationError

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


def _field_summary(exc: Exception) -> str:
    """Condense a pydantic ValidationError into a short, model-actionable phrase."""
    errors_fn = getattr(exc, "errors", None)
    if callable(errors_fn):
        parts: list[str] = []
        for err in exc.errors():
            loc = ".".join(str(x) for x in err.get("loc", ())) or "(root)"
            parts.append(f"{loc}: {err.get('msg', 'invalid')}")
        if parts:
            return "; ".join(parts[:5])
    return str(exc)[:200]


def _extract_first_json(text: str) -> str:
    """
    Extract the first complete JSON object or array from text that may have
    trailing prose after the closing bracket.

    GPT-5.x models with json_schema response_format sometimes append a newline
    and explanatory text after the JSON object they return. Pydantic's
    model_validate_json (backed by Rust serde_json) is strict — any character
    after the closing } is a parse failure. This strips the trailer.

    Strategy: find the first { or [, then walk forward tracking depth until the
    matching closer. Return only that slice. Falls back to the full text on any
    scan error so the caller's ValidationError still fires with useful context.
    """
    text = text.strip()
    if not text:
        return text

    # Find the opening bracket
    start = -1
    opener, closer = "", ""
    for i, ch in enumerate(text):
        if ch == "{":
            opener, closer = "{", "}"
            start = i
            break
        if ch == "[":
            opener, closer = "[", "]"
            start = i
            break

    if start == -1:
        return text  # no JSON structure found; let the caller handle it

    depth = 0
    in_string = False
    escape_next = False
    for i in range(start, len(text)):
        ch = text[i]
        if escape_next:
            escape_next = False
            continue
        if ch == "\\" and in_string:
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return text[start : i + 1]

    # Unbalanced — return from start; repair/validate will handle it
    return text[start:]


def parse_llm_json(content: str | None, model: type[T]) -> T:
    """
    Parse LLM output into a Pydantic model with automatic repair.

    Pipeline:
      1. Strip code fences and extract the first complete JSON object/array
         (handles GPT-5.x appending prose after the closing brace).
      2. Run json_repair to fix structural issues (trailing commas, unquoted
         keys, truncated objects).
      3. Validate against the Pydantic model.

    Raises OutputValidationError on all failures — never propagates raw
    ValidationError — so _run_state has one exception type to classify.
    """
    if not content or not content.strip():
        raise OutputValidationError("model returned empty content")

    # Step 1: extract the JSON portion (strips trailing prose from GPT-5.x)
    extracted = _extract_first_json(content)

    # Step 2: repair structural issues
    try:
        from json_repair import repair_json  # type: ignore[import]
        repaired: Any = repair_json(extracted, return_objects=False)
        if not isinstance(repaired, str):
            repaired = extracted
    except Exception:
        repaired = extracted

    # Step 3: validate
    try:
        return model.model_validate_json(repaired)
    except (ValidationError, ValueError) as first_exc:
        # Last-ditch: try the raw content in case extraction/repair made it worse
        if repaired != content:
            try:
                return model.model_validate_json(content)
            except (ValidationError, ValueError):
                pass

        detail = _field_summary(first_exc)
        logger.warning(
            "parse_llm_json failed for %s: %s (raw length=%d)",
            model.__name__,
            detail,
            len(content),
        )
        raise OutputValidationError(detail, raw=content) from first_exc
