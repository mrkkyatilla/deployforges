"""Exceptions that cross the async pipeline boundary into Celery."""


class TransientGeminiError(RuntimeError):
    """Gemini returned overload / quota signals; safe to retry the whole pipeline."""
