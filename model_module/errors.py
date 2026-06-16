"""
Typed errors for the model client layer.

Separates retryable transport failures (timeouts, rate limits, server errors)
from terminal failures (bad request, auth) so the caller can decide whether to
retry without inspecting raw exception messages.
"""


class ModelError(Exception):
    """
    Raised by ArkModelLink when a model call fails.

    Args:
        message: Human-readable description of the failure.
        retryable: True for transport failures the caller should retry
            (timeout, 429, 5xx). False for failures where retrying will
            not help (400, 401, 403).
        cause: The original exception, preserved for logging.
    """

    def __init__(self, message: str, *, retryable: bool, cause: Exception | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.cause = cause


class OutputValidationError(Exception):
    """
    Raised when the model responded but its output doesn't match the required
    schema (missing field, bad JSON, truncated object). Distinct from ModelError
    which signals a transport/API failure.

    detail is model-actionable (e.g. "final: Field required") and safe to feed
    back into context. raw is the failing string — logged only, never shown.
    """

    def __init__(self, detail: str, *, raw: str | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.raw = raw
