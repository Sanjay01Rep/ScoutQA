"""Types every model provider shares. A provider's job is narrow: turn `messages` + a JSON schema into
raw text. Parsing, validation, retries, repair, caching and usage accounting all live in `llm/router.py`,
so every provider behaves identically from the caller's point of view.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel

from scoutqa.errors import ScoutQAError

T = TypeVar("T", bound=BaseModel)

Role = Literal["system", "user", "assistant"]


@dataclass(frozen=True)
class Message:
    role: Role
    content: str
    cacheable: bool = False  # hint: this message is a stable prefix a provider may cache server-side


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0  # counted separately; already included in input_tokens

    def __add__(self, other: Usage) -> Usage:
        return Usage(self.input_tokens + other.input_tokens, self.output_tokens + other.output_tokens,
                    self.cached_input_tokens + other.cached_input_tokens)


@dataclass(frozen=True)
class RawCompletion:
    """What a provider adapter returns: unparsed model output plus what it cost."""

    text: str
    usage: Usage
    latency_s: float


@dataclass
class Generation(Generic[T]):
    """What the router returns: a validated value plus everything needed to trace and account for it."""

    value: T
    usage: Usage
    text: str
    latency_s: float
    provider: str
    model: str
    stage: str
    cached: bool = False  # served from ScoutQA's own response cache; usage is zero when True
    attempts: int = 1  # transport retries + JSON/validation repair attempts, combined


class ProviderError(ScoutQAError):
    """A model provider request failed: network, auth, rate limit, refusal, or an unusable response."""


class BudgetExceeded(ScoutQAError):
    """A configured token or cost budget would be exceeded by this call."""


class SchemaError(ScoutQAError):
    """The requested output type cannot be represented as portable JSON Schema."""


class ModelClient:
    """Base class for a provider transport. Subclasses implement `complete`; nothing else."""

    provider: str

    def __init__(self, model: str) -> None:
        self.model = model

    async def complete(self, messages: list[Message], schema: dict[str, Any], *, temperature: float,
                       max_output_tokens: int) -> RawCompletion:
        raise NotImplementedError

    async def aclose(self) -> None:
        return None
