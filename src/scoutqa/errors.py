"""Typed exceptions. Messages must never contain secret values."""


class ScoutQAError(Exception):
    """Base class for all ScoutQA errors."""


class ConfigError(ScoutQAError):
    """Invalid or missing configuration."""


class SecretError(ConfigError):
    """A referenced secret (env var / keyring entry) is missing."""


class AuthError(ScoutQAError):
    """Login failed, the session is invalid, or interactive login is required."""


class CrawlError(ScoutQAError):
    """The crawl could not start or was aborted."""
