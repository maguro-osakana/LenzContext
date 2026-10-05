"""Bounded LLM concurrency with metadata and ordered output on the caller thread."""

import logging
import time
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Literal

from .exif import InvalidJPEG, is_jpeg_path
from .llm.base import AnalysisCancelled, LLMError
from .models import BatchResult, ImageResult
from .pipeline import Pipeline, PreparedImage

LOG = logging.getLogger(__name__)


@dataclass
class _Outcome:
    status: Literal["success", "skipped", "failed"]
    image: ImageResult | None = None


def _failure(path: Path | str, exc: Exception) -> _Outcome:
    if isinstance(exc, (OSError, InvalidJPEG)):
        LOG.warning("skipping unreadable JPEG: %s", path)
        return _Outcome("skipped")
    if isinstance(exc, LLMError):
        LOG.error("analysis failed for %s: %s", path, exc)
    else:
        # Custom analyzers may put credentials or image data in exception messages.
        LOG.error("processing failed for %s (%s)", path, type(exc).__name__)
    return _Outcome("failed")


def process_batch(paths: list[Path | str], pipeline: Pipeline,
                     on_success: Callable[[ImageResult], None] | None = None, *,
                     jobs: int = 1, stop_event: Event | None = None) -> BatchResult:
    if type(jobs) is not int or jobs < 1:
        raise ValueError("jobs must be a positive integer")
    if not paths:
        LOG.debug("batch summary: 0 succeeded, 0 skipped, 0 failed")
        return BatchResult(images=[])
    stop = stop_event if stop_event is not None else Event()
    workers = min(jobs, len(paths))
    window = 2 * workers
    pending: dict[Future[ImageResult], tuple[int, float]] = {}
    ready: dict[int, _Outcome] = {}
    images = []
    next_input = next_output = skipped = failed = 0

    def analyze(prepared: PreparedImage) -> ImageResult:
        if stop.is_set():
            raise AnalysisCancelled("batch cancelled")
        return pipeline.analyze(prepared)

    executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="lenzcontext")
    try:
        while next_output < len(paths):
            if stop.is_set():
                raise AnalysisCancelled("batch cancelled")
            # Handle writes before submitting more work: callback failures stop the batch.
            while next_output in ready:
                if stop.is_set():
                    raise AnalysisCancelled("batch cancelled")
                outcome = ready.pop(next_output)
                if outcome.image is not None:
                    if on_success is not None:
                        on_success(outcome.image)
                    images.append(outcome.image)
                elif outcome.status == "skipped":
                    skipped += 1
                else:
                    failed += 1
                next_output += 1

            while (next_input < len(paths) and len(pending) < workers
                   and len(pending) + len(ready) < window):
                if stop.is_set():
                    raise AnalysisCancelled("batch cancelled")
                index = next_input
                path = paths[index]
                next_input += 1
                if not is_jpeg_path(Path(path)):
                    LOG.warning("skipping non-JPEG file: %s", path)
                    ready[index] = _Outcome("skipped")
                    continue
                started = time.perf_counter()
                try:
                    prepared = pipeline.prepare(path)
                except AnalysisCancelled:
                    raise
                except Exception as exc:
                    ready[index] = _failure(path, exc)
                    LOG.info("processing %s (%.2fs)", path, time.perf_counter() - started)
                else:
                    future = executor.submit(analyze, prepared)
                    pending[future] = (index, started)
                    del prepared

            if next_output in ready:
                continue
            if pending:
                done, _ = wait(pending, return_when=FIRST_COMPLETED)
                for future in done:
                    index, started = pending.pop(future)
                    try:
                        ready[index] = _Outcome("success", future.result())
                    except AnalysisCancelled:
                        raise
                    except Exception as exc:
                        ready[index] = _failure(paths[index], exc)
                    finally:
                        LOG.info("processing %s (%.2fs)", paths[index], time.perf_counter() - started)
                # Drop completed futures, including their results/tracebacks, before refilling.
                done.clear()
                del future
    except BaseException:
        stop.set()
        raise
    finally:
        executor.shutdown(wait=True, cancel_futures=True)

    LOG.debug("batch summary: %d succeeded, %d skipped, %d failed", len(images), skipped, failed)
    return BatchResult(images=images)
