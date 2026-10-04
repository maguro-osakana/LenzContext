from typing import Protocol

from ..models import Address, AnalysisResult


class LLMError(RuntimeError):
    """Safe-to-log failure, without credentials or provider response contents."""


class AnalysisCancelled(LLMError):
    """Cooperative cancellation; must not trigger analysis retries."""


class VisionAnalyzer(Protocol):
    def analyze(self, jpeg: bytes, address: Address | None, taken_at: str | None) -> AnalysisResult: ...
