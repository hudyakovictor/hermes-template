"""Exceptions raised by the optional Bottom Detection research layer."""

from __future__ import annotations


class SkillError(RuntimeError):
    """Base class for recoverable skill errors."""


class MCPError(SkillError):
    """An MCP adapter failed after the configured retries."""


class ExperimentError(SkillError):
    """An experiment hand-off or experiment evaluator failed."""
