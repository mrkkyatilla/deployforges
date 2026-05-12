from __future__ import annotations


class DeployForgeError(Exception):
    """Base exception for all DeployForge SDK errors."""


class AuthenticationError(DeployForgeError):
    """Raised when the API key is invalid or missing."""


class RateLimitError(DeployForgeError):
    """Raised when the API rate limit has been exceeded."""


class ProjectNotFoundError(DeployForgeError):
    """Raised when a project cannot be found."""


class InsufficientCreditsError(DeployForgeError):
    """Raised when the account has insufficient credits."""
