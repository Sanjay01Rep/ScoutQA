"""Pydantic models for `scoutqa.yaml`.

Credentials are referenced by environment-variable *name* only; values are resolved at runtime by
`scoutqa.config.secrets` and never stored on these models.
"""

from __future__ import annotations

import re
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ScopeConfig(_Strict):
    allowed_domains: list[str] = Field(
        default_factory=list,
        description="Hosts the crawler may visit. Empty = the base_url host. Supports '*.example.com'.",
    )
    include: list[str] = Field(default_factory=list, description="URL globs; if set, a URL must match one.")
    exclude: list[str] = Field(default_factory=list, description="URL globs that are never visited.")
    start_urls: list[str] = Field(default_factory=list, description="Extra entry points besides base_url.")
    max_depth: int = Field(default=3, ge=0)
    max_pages: int = Field(default=50, ge=1)
    max_instances_per_pattern: int = Field(
        default=3, ge=1, description="Visit at most N URLs per pattern such as /items/{id}."
    )
    max_duration_s: float = Field(default=600, gt=0)
    politeness_delay_ms: int = Field(default=0, ge=0)
    explore_actions: bool = Field(
        default=True, description="Click safe in-page controls (tabs, menus, dialog openers) to find UI states."
    )
    max_actions_per_state: int = Field(default=8, ge=0)


class SuccessCondition(_Strict):
    """How to tell that login worked. Unset fields are ignored; all set fields must hold."""

    url_regex: str | None = None
    selector: str | None = None

    @field_validator("url_regex")
    @classmethod
    def _valid_regex(cls, v: str | None) -> str | None:
        if v is not None:
            re.compile(v)
        return v


def _check_env_name(v: str | None) -> str | None:
    if v is not None and not _ENV_NAME.match(v):
        raise ValueError("must be the NAME of an environment variable (e.g. APP_PASSWORD), not the secret itself")
    return v


class ProfileConfig(_Strict):
    """Credentials of one user role, by env-var name."""

    username_env: str
    password_env: str

    @field_validator("username_env", "password_env")
    @classmethod
    def _env_name(cls, v: str) -> str:
        _check_env_name(v)
        return v


DEFAULT_PROFILE = "default"
_PROFILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")


class AuthConfig(_Strict):
    type: Literal["none", "form"] = "none"
    login_url: str | None = None
    username_env: str | None = Field(default=None, description="Name of the env var holding the username.")
    password_env: str | None = Field(default=None, description="Name of the env var holding the password.")
    profiles: dict[str, ProfileConfig] = Field(
        default_factory=dict,
        description="Several user roles, e.g. {admin: {...}, viewer: {...}}. Replaces username_env/password_env.",
    )
    username_selector: str | None = None
    password_selector: str | None = None
    submit_selector: str | None = None
    success: SuccessCondition = Field(default_factory=SuccessCondition)
    check_url: str | None = Field(
        default=None, description="Protected page used to validate a saved session. Default: base_url."
    )
    session_max_age_hours: float = Field(default=12, gt=0)
    max_reauth: int = Field(default=2, ge=0, description="Re-logins allowed during one crawl.")

    @field_validator("username_env", "password_env")
    @classmethod
    def _env_name(cls, v: str | None) -> str | None:
        return _check_env_name(v)

    @field_validator("profiles")
    @classmethod
    def _profile_names(cls, v: dict[str, ProfileConfig]) -> dict[str, ProfileConfig]:
        for name in v:
            if not _PROFILE_NAME.match(name):
                raise ValueError(f"invalid profile name {name!r} (letters, digits, - and _)")
        return v

    @model_validator(mode="after")
    def _form_needs_fields(self) -> AuthConfig:
        if self.type == "form":
            missing = ["login_url"] if self.login_url is None else []
            if not self.profiles:
                missing += [n for n in ("username_env", "password_env") if getattr(self, n) is None]
            if missing:
                raise ValueError(f"auth.type 'form' requires: {', '.join(missing)} (or auth.profiles)")
        return self

    def profile_names(self) -> list[str]:
        """Roles to crawl as. Without explicit profiles there is one role, 'default'."""
        return list(self.profiles) or [DEFAULT_PROFILE]

    def credentials(self, profile: str) -> ProfileConfig:
        if self.profiles:
            if profile not in self.profiles:
                raise ValueError(f"unknown profile {profile!r}; configured: {', '.join(self.profiles)}")
            return self.profiles[profile]
        if profile != DEFAULT_PROFILE:
            raise ValueError(f"unknown profile {profile!r}; no auth.profiles are configured")
        assert self.username_env and self.password_env  # validated above
        return ProfileConfig(username_env=self.username_env, password_env=self.password_env)


class SafetyConfig(_Strict):
    read_only: bool = Field(default=True, description="Block every non-GET/HEAD/OPTIONS request.")
    allow_mutation_patterns: list[str] = Field(
        default_factory=list, description="URL globs allowed to receive POST/PUT/PATCH/DELETE."
    )
    allow_graphql_queries: bool = Field(
        default=True, description="Let GraphQL POSTs through when they contain only queries."
    )
    extra_unsafe_keywords: list[str] = Field(
        default_factory=list, description="Extra words that mark a link/button as destructive."
    )
    allow_link_patterns: list[str] = Field(
        default_factory=list,
        description="URL globs of links to follow even if their wording looks unsafe (e.g. an /archive page).",
    )


class SettleConfig(_Strict):
    quiet_ms: int = Field(default=500, ge=0, description="Network + DOM must be quiet this long.")
    timeout_ms: int = Field(default=10_000, ge=0)
    lazy_scroll_steps: int = Field(default=5, ge=0, description="Scroll steps to trigger lazy loading.")


ResourceType = Literal["image", "media", "font", "stylesheet"]
_DEFAULT_BLOCKED: tuple[ResourceType, ...] = ("image", "media", "font")


class BrowserConfig(_Strict):
    headless: bool = True
    block_resources: list[ResourceType] = Field(
        default_factory=lambda: list(_DEFAULT_BLOCKED),
        description="Resource types skipped for speed; the DOM still has alt text etc.",
    )
    viewport_width: int = 1366
    viewport_height: int = 768
    navigation_timeout_ms: int = Field(default=30_000, gt=0)
    ignore_https_errors: bool = False
    settle: SettleConfig = Field(default_factory=SettleConfig)


RulePack = Literal["fields", "forms", "actions", "navigation", "tables", "dialogs", "auth", "access", "smoke", "api"]
ALL_RULE_PACKS: tuple[RulePack, ...] = (
    "fields", "forms", "actions", "navigation", "tables", "dialogs", "auth", "access", "smoke", "api",
)


class GenerationConfig(_Strict):
    rule_packs: list[RulePack] = Field(default_factory=lambda: list(ALL_RULE_PACKS))
    modules: dict[str, list[str]] = Field(
        default_factory=dict,
        description="Module name -> URL-pattern globs, e.g. {Orders: ['/orders*', '/cart*']}. "
                    "Unmatched pages get a module from their first path segment.",
    )


class TemplateConfig(_Strict):
    """The user's test case template. Without `path` the built-in default template is used."""

    path: str | None = Field(default=None, description=".xlsx, .csv, .md or .json template file")
    sheet: str | None = Field(default=None, description="Excel sheet holding the header row (default: first)")
    header_row: int | None = Field(default=None, ge=1, description="1-based header row (default: auto-detect)")
    layout: Literal["auto", "case_per_row", "step_per_row"] = "auto"
    columns: dict[str, str] = Field(
        default_factory=dict,
        description="Override the mapping: column header -> canonical field (id, module, title, scenario, "
                    "preconditions, steps, expected_result, priority, type, test_data, roles, tags, page, "
                    "notes, review_status, step_no, step_action, step_expected, step_data) or 'blank'.",
    )
    defaults: dict[str, str] = Field(default_factory=dict, description="Fixed value per column, e.g. {Env: QA}")


ModelProvider = Literal["anthropic", "openai", "azure_openai", "gemini", "fake"]
JsonMode = Literal["schema", "object", "none"]
_PROFILE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


class ModelProfile(_Strict):
    """One callable model: a provider, a model/deployment name, and how to reach it."""

    provider: ModelProvider
    model: str = Field(description="Model name (or, for azure_openai, the deployment name).")
    api_key_env: str | None = Field(
        default=None, description="Name of the env var holding the API key. Optional only for 'openai' "
                                  "profiles with a base_url pointing at a keyless local server."
    )
    base_url: str | None = Field(
        default=None, description="'openai' provider only: any OpenAI-compatible endpoint "
                                  "(Ollama, vLLM, LM Studio, Groq, DeepSeek, Together, OpenRouter, ...)."
    )
    azure_endpoint: str | None = Field(default=None, description="'azure_openai' provider only.")
    azure_api_version: str = "2026-01-01-preview"
    json_mode: JsonMode = Field(
        default="schema", description="'openai'/'azure_openai' only: how strictly to enforce the output "
                                      "shape. Use 'object' or 'none' for servers with partial support."
    )
    temperature: float = Field(default=0.2, ge=0, le=2)
    max_output_tokens: int = Field(default=4096, gt=0, le=1_000_000)
    extra_headers: dict[str, str] = Field(default_factory=dict)

    @field_validator("api_key_env")
    @classmethod
    def _env_name(cls, v: str | None) -> str | None:
        return _check_env_name(v)

    @model_validator(mode="after")
    def _requirements(self) -> ModelProfile:
        if self.provider == "azure_openai" and not self.azure_endpoint:
            raise ValueError("provider 'azure_openai' requires azure_endpoint")
        if self.provider != "azure_openai" and self.azure_endpoint:
            raise ValueError("azure_endpoint is only used by provider 'azure_openai'")
        if self.provider in ("anthropic", "gemini", "azure_openai") and not self.api_key_env:
            raise ValueError(f"provider {self.provider!r} requires api_key_env")
        if self.provider == "openai" and not self.api_key_env and not self.base_url:
            raise ValueError("provider 'openai' without a base_url talks to api.openai.com and needs api_key_env "
                             "(set base_url for a keyless local server, e.g. Ollama/vLLM)")
        if self.provider != "openai" and self.base_url:
            raise ValueError("base_url is only used by provider 'openai' "
                             "(it is how any OpenAI-compatible endpoint is reached)")
        if self.provider not in ("openai", "azure_openai") and self.json_mode != "schema":
            raise ValueError("json_mode only applies to provider 'openai' / 'azure_openai'")
        return self


class ModelConfig(_Strict):
    """LLM setup: which providers/models are available and which stage uses which (docs/ARCHITECTURE.md §3.11)."""

    profiles: dict[str, ModelProfile] = Field(default_factory=dict)
    default_profile: str | None = Field(default=None, description="Profile used by any stage not in `routing`.")
    routing: dict[str, str] = Field(
        default_factory=dict, description="Generation stage -> profile name, e.g. {scenarios: cheap, expand: strong}."
    )
    cache: bool = Field(default=True, description="Reuse a previous response for an identical call (0 tokens).")
    max_retries: int = Field(default=3, ge=0, le=10, description="Retries on a transport failure (network/429/5xx).")
    max_repair_attempts: int = Field(
        default=2, ge=0, le=5, description="Extra attempts after invalid JSON / schema-validation failure."
    )
    concurrency: int = Field(default=4, ge=1, le=32, description="Calls in flight at once, across all stages.")
    budget_usd: float | None = Field(default=None, gt=0, description="Stop once estimated spend reaches this.")
    budget_tokens: int | None = Field(default=None, gt=0, description="Stop once input+output tokens reach this.")
    pricing: dict[str, tuple[float, float, float]] = Field(
        default_factory=dict,
        description="Override/add prices: 'provider:model' -> [input, cached_input, output] USD per 1M tokens.",
    )

    @field_validator("profiles")
    @classmethod
    def _profile_names(cls, v: dict[str, ModelProfile]) -> dict[str, ModelProfile]:
        for name in v:
            if not _PROFILE_ID.match(name):
                raise ValueError(f"invalid model profile name {name!r} (letters, digits, - and _)")
        return v

    @model_validator(mode="after")
    def _routes_resolve(self) -> ModelConfig:
        names = {n for n in (self.default_profile, *self.routing.values()) if n is not None}
        missing = names - set(self.profiles)
        if missing:
            raise ValueError(
                f"llm.routing/default_profile refers to undefined profile(s): {', '.join(sorted(missing))}"
            )
        return self

    def profile_name_for(self, stage: str) -> str:
        name = self.routing.get(stage, self.default_profile)
        if name is None:
            raise ValueError(
                f"No model configured for stage {stage!r}. Set llm.default_profile or llm.routing.{stage} "
                f"in scoutqa.yaml (profiles defined: {', '.join(self.profiles) or 'none'})."
            )
        return name

    def profile_for(self, stage: str) -> ModelProfile:
        return self.profiles[self.profile_name_for(stage)]


class ProjectConfig(_Strict):
    project: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
    base_url: str
    scope: ScopeConfig = Field(default_factory=ScopeConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    generation: GenerationConfig = Field(default_factory=GenerationConfig)
    template: TemplateConfig = Field(default_factory=TemplateConfig)
    llm: ModelConfig = Field(default_factory=ModelConfig)

    @field_validator("base_url")
    @classmethod
    def _http_url(cls, v: str) -> str:
        parts = urlsplit(v)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("base_url must be an absolute http(s) URL")
        return v

    @property
    def base_host(self) -> str:
        host = urlsplit(self.base_url).hostname
        assert host is not None  # guaranteed by the validator
        return host

    @property
    def allowed_domains(self) -> list[str]:
        return self.scope.allowed_domains or [self.base_host]
