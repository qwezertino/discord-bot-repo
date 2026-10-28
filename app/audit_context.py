"""Request attribution; Gateway/background actions have no human actor."""
from contextvars import ContextVar

audit_actor: ContextVar[str | None] = ContextVar("audit_actor", default=None)
