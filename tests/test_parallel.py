from threading import Barrier, Event, Lock, get_ident

import pytest
import yaml
from PIL import Image

import lenzcontext.batch as parallel
from lenzcontext.cli import main, process_batch
from lenzcontext.config import ConfigError, Prompts, Settings
from lenzcontext.exif import InvalidJPEG
from lenzcontext.llm.base import AnalysisCancelled, LLMError
from lenzcontext.llm.openai_compatible import OpenAICompatibleAnalyzer, TransportError
from lenzcontext.models import ExifInfo, FileInfo, ImageResult
from lenzcontext.pipeline import PreparedImage
from lenzcontext.geonames.reverse import ReverseGeocoder


class StubPipeline:
    def __init__(self, analysis, run=None):
        self.analysis = analysis
        self.run = run
        self.owner = get_ident()
        self.prepared = []

    def prepare(self, path):
        assert get_ident() == self.owner
        self.prepared.append(str(path))
        if str(path) == "broken.jpg":
            raise InvalidJPEG("broken")
        return PreparedImage(str(path), str(path).encode(), ExifInfo(), None)

    def analyze(self, prepared):
        assert get_ident() != self.owner
        if self.run is not None:
            self.run(prepared.input_name)
        return ImageResult(file=FileInfo(name=prepared.input_name), exif=prepared.exif,
                           address=prepared.address, analysis=self.analysis)


def test_parallel_overlap_limit_and_caller_thread_output(analysis):
    barrier = Barrier(2)
    lock = Lock()
    active = peak = 0

    def run(name):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        barrier.wait(timeout=5)
        with lock:
            active -= 1

    pipeline = StubPipeline(analysis, run)
    written = []

    def write(image):
        assert get_ident() == pipeline.owner
        written.append(image.file.name)

    paths = [f"{i}.jpg" for i in range(8)]
    result = process_batch(paths, pipeline, write, jobs=2)
    assert peak == 2
    assert written == paths
    assert [x.file.name for x in result.images] == paths


def test_reversed_completion_failures_duplicates_and_refill(monkeypatch, analysis, caplog):
    release = Event()

    def run(name):
        if name == "same.jpg":
            assert release.wait(5)
        if name == "failed.jpg":
            raise LLMError("LLM API returned HTTP 500")

    pipeline = StubPipeline(analysis, run)
    real_wait = parallel.wait
    saw_refill = False

    def wait(futures, **kwargs):
        nonlocal saw_refill
        # A slow first image must not prevent preparing a replacement for the failed second.
        if "other.jpg" in pipeline.prepared:
            saw_refill = True
            release.set()
        return real_wait(futures, **kwargs)

    monkeypatch.setattr(parallel, "wait", wait)
    paths = ["same.jpg", "failed.jpg", "other.jpg", "not-jpeg.png", "broken.jpg", "same.jpg"]
    result = process_batch(paths, pipeline, jobs=2)
    assert saw_refill
    assert [x.file.name for x in result.images] == ["same.jpg", "other.jpg", "same.jpg"]
    assert "500" in caplog.text
    assert "skipping non-JPEG" in caplog.text
    assert "skipping unreadable JPEG" in caplog.text


def test_outstanding_window_is_bounded(monkeypatch, analysis):
    release = Event()

    def run(name):
        if name == "0.jpg":
            assert release.wait(5)

    pipeline = StubPipeline(analysis, run)
    real_wait = parallel.wait
    observed_window = False

    def wait(futures, **kwargs):
        nonlocal observed_window
        if not observed_window and len(pipeline.prepared) >= 4 and len(futures) == 1:
            # Only the first slow image remains. The other window slots are completed results.
            release.set()
            assert len(pipeline.prepared) == 4
            observed_window = True
        return real_wait(futures, **kwargs)

    monkeypatch.setattr(parallel, "wait", wait)
    paths = [f"{i}.jpg" for i in range(20)]
    result = process_batch(paths, pipeline, jobs=2)
    assert observed_window
    assert [x.file.name for x in result.images] == paths


@pytest.mark.parametrize("jobs", [1, 2])
def test_callback_failure_stops_refill_and_signals_running_work(analysis, jobs):
    stop = Event()
    second_started = Event()

    def run(name):
        if name == "first.jpg":
            if jobs > 1:
                assert second_started.wait(5)
        else:
            second_started.set()
            assert stop.wait(5)
            raise AnalysisCancelled("cancelled")

    pipeline = StubPipeline(analysis, run)

    def write(image):
        raise OSError("disk full")

    with pytest.raises(OSError, match="disk full"):
        process_batch(["first.jpg", "second.jpg", "third.jpg"], pipeline, write,
                      jobs=jobs, stop_event=stop)
    assert stop.is_set()
    assert pipeline.prepared == (["first.jpg"] if jobs == 1 else ["first.jpg", "second.jpg"])


def test_interrupt_signals_running_work(monkeypatch, analysis):
    stop = Event()
    started = Event()

    def run(name):
        started.set()
        assert stop.wait(5)
        raise AnalysisCancelled("cancelled")

    pipeline = StubPipeline(analysis, run)

    def interrupt(*args, **kwargs):
        assert started.wait(5)
        raise KeyboardInterrupt

    monkeypatch.setattr(parallel, "wait", interrupt)
    with pytest.raises(KeyboardInterrupt):
        process_batch(["first.jpg", "second.jpg", "third.jpg"], pipeline,
                      jobs=2, stop_event=stop)
    assert stop.is_set()
    assert len(pipeline.prepared) == 2


@pytest.mark.parametrize("failure", ["invalid", "transport", "success"])
def test_cancellation_stops_infinite_retries(analysis, failure):
    stop = Event()
    calls = []

    def transport(*args):
        calls.append(1)
        stop.set()
        if failure == "transport":
            raise TransportError("failed", 500)
        return {"choices": [{"message": {"content": (
            analysis.model_dump_json() if failure == "success" else "invalid")}}]}

    analyzer = OpenAICompatibleAnalyzer(Settings(api_base="http://localhost", model="test", retries=0),
        Prompts("system", "user"), transport, stop_event=stop)
    with pytest.raises(AnalysisCancelled):
        analyzer.analyze(b"jpeg", None, None)
    assert len(calls) == 1


def test_pre_cancelled_analyzer_sends_no_request():
    stop = Event()
    stop.set()
    def transport(*args):
        pytest.fail("must not send a request")
    analyzer = OpenAICompatibleAnalyzer(Settings(api_base="http://localhost", model="test"),
        Prompts("system", "user"), transport, stop_event=stop)
    with pytest.raises(AnalysisCancelled):
        analyzer.analyze(b"jpeg", None, None)


@pytest.mark.parametrize("jobs", [0, -1, True, 1.5, "2", None])
def test_invalid_jobs_settings(jobs):
    with pytest.raises(ConfigError, match="jobs"):
        Settings(api_base="http://localhost", model="test", jobs=jobs)


def test_jobs_precedence_and_defaults(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LENZCONTEXT_JOBS", raising=False)
    monkeypatch.setenv("LENZCONTEXT_MODEL", "test")
    assert Settings.from_env().jobs == 1
    (tmp_path / ".env").write_text("LENZCONTEXT_JOBS=4\n")
    assert Settings.from_env().jobs == 4
    monkeypatch.setenv("LENZCONTEXT_JOBS", "2")
    assert Settings.from_env().jobs == 2
    monkeypatch.setenv("LENZCONTEXT_JOBS", "invalid")
    assert Settings.from_env(jobs=3).jobs == 3
    with pytest.raises(ConfigError, match="JOBS"):
        Settings.from_env()


@pytest.mark.parametrize("jobs", ["0", "-1", "1.5", ""])
def test_invalid_environment_jobs(monkeypatch, tmp_path, jobs):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LENZCONTEXT_MODEL", "test")
    monkeypatch.setenv("LENZCONTEXT_JOBS", jobs)
    with pytest.raises(ConfigError):
        Settings.from_env()


@pytest.mark.parametrize("flag", [None, "-j", "--jobs"])
def test_cli_jobs_and_order(monkeypatch, tmp_path, make_jpeg, analysis, flag):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LENZCONTEXT_MODEL", "test")
    monkeypatch.setenv("LENZCONTEXT_JOBS", "invalid" if flag else "2")
    barrier = Barrier(2)
    def analyze(self, *args):
        assert self.settings.jobs == 2
        barrier.wait(timeout=5)
        return analysis
    monkeypatch.setattr(OpenAICompatibleAnalyzer, "analyze", analyze)
    paths = [make_jpeg(f"{i}.jpg") for i in range(2)]
    output = tmp_path / "result.yaml"
    jobs_args = [flag, "2"] if flag else []
    assert main([*(str(p) for p in paths), *jobs_args, "-o", str(output)]) == 0
    assert [x['file']['name'] for x in yaml.safe_load(output.read_text())['images']] == [str(p) for p in paths]


def test_cli_interrupt_returns_130(monkeypatch, tmp_path, make_jpeg):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LENZCONTEXT_MODEL", "test")
    monkeypatch.delenv("LENZCONTEXT_JOBS", raising=False)
    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt
    monkeypatch.setattr("lenzcontext.cli.process_batch", interrupt)
    output = tmp_path / "result.yaml"
    assert main([str(make_jpeg()), "--jobs", "2", "-o", str(output)]) == 130
    assert not output.exists()


@pytest.mark.parametrize("jobs", ["0", "-2"])
def test_invalid_cli_jobs_fails_before_api(monkeypatch, tmp_path, make_jpeg, jobs):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LENZCONTEXT_MODEL", "test")
    def analyze(*args):
        pytest.fail("must not analyze an image")
    monkeypatch.setattr(OpenAICompatibleAnalyzer, "analyze", analyze)
    output = tmp_path / "result.yaml"
    assert main([str(make_jpeg()), "--jobs", jobs, "-o", str(output)]) == 2
    assert not output.exists()


def test_parallel_real_geocoder_stays_on_caller_thread(monkeypatch, tmp_path, make_jpeg, geonames_db, analysis):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LENZCONTEXT_MODEL", "test")
    owner = get_ident()
    barrier = Barrier(2)
    original_reverse = ReverseGeocoder.reverse
    calls = []

    def reverse(self, *coords):
        assert get_ident() == owner
        calls.append(coords)
        return original_reverse(self, *coords)

    def analyze(self, *args):
        assert get_ident() != owner
        barrier.wait(timeout=5)
        return analysis

    monkeypatch.setattr(ReverseGeocoder, "reverse", reverse)
    monkeypatch.setattr(OpenAICompatibleAnalyzer, "analyze", analyze)
    exif = Image.Exif()
    exif[34853] = {1: "N", 2: (37, 33, 59.4), 3: "E", 4: (126, 58, 40.8)}
    paths = [make_jpeg(f"{i}.jpg", exif=exif) for i in range(2)]
    output = tmp_path / "result.yaml"
    assert main([*(str(p) for p in paths), "--jobs", "2", "--geonames-db", str(geonames_db),
                 "-o", str(output)]) == 0
    assert len(calls) == 2
    assert all(x['address'] is not None for x in yaml.safe_load(output.read_text())['images'])


def test_parallel_all_failures_leave_output_untouched(monkeypatch, tmp_path, make_jpeg):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LENZCONTEXT_MODEL", "test")
    def analyze(*args):
        raise LLMError("failed")
    monkeypatch.setattr(OpenAICompatibleAnalyzer, "analyze", analyze)
    paths = [make_jpeg(f"{i}.jpg") for i in range(3)]
    output = tmp_path / "result.yaml"
    output.write_text("existing output")
    assert main([*(str(p) for p in paths), "--jobs", "2", "-o", str(output)]) == 1
    assert output.read_text() == "existing output"
    output.unlink()
    assert main([*(str(p) for p in paths), "--jobs", "2", "-o", str(output)]) == 1
    assert not output.exists()


def test_parallel_more_jobs_than_inputs_and_empty_batch(analysis):
    pipeline = StubPipeline(analysis)
    assert [x.file.name for x in process_batch(["one.jpg"], pipeline, jobs=100).images] == ["one.jpg"]
    assert process_batch([], pipeline, jobs=100).images == []


def test_parallel_stopped_event_and_non_jpegs(analysis):
    pipeline = StubPipeline(analysis)
    assert process_batch(["a.png", "b.txt"], pipeline, jobs=2).images == []
    assert pipeline.prepared == []
    stop = Event()
    stop.set()
    with pytest.raises(AnalysisCancelled):
        process_batch(["one.jpg"], pipeline, jobs=2, stop_event=stop)
    assert pipeline.prepared == []


def test_parallel_cli_append_keeps_input_order(monkeypatch, tmp_path, make_jpeg, analysis):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LENZCONTEXT_MODEL", "test")
    monkeypatch.setattr(OpenAICompatibleAnalyzer, "analyze", lambda *args: analysis)
    paths = [make_jpeg(f"{i}.jpg") for i in range(3)]
    output = tmp_path / "result.yaml"
    assert main([str(paths[0]), "-j", "2", "-o", str(output)]) == 0
    assert main([*(str(p) for p in paths[1:]), "-j", "2", "-a", str(output)]) == 0
    assert [x['file']['name'] for x in yaml.safe_load(output.read_text())['images']] == [str(p) for p in paths]


@pytest.mark.parametrize("jobs", [1, 2])
def test_batch_uses_workers_even_with_one_job(analysis, jobs):
    pipeline = StubPipeline(analysis)
    owner = get_ident()
    written = []

    def write(image):
        assert get_ident() == owner
        written.append(image.file.name)

    result = process_batch(["first.jpg", "second.jpg", "third.jpg"], pipeline, write, jobs=jobs)
    assert written == ["first.jpg", "second.jpg", "third.jpg"]
    assert [image.file.name for image in result.images] == written


@pytest.mark.parametrize("jobs", [1, 4])
@pytest.mark.parametrize("fails", [False, True])
def test_analysis_timing_divides_by_jobs_and_excludes_other_work(monkeypatch, analysis, caplog, jobs, fails):
    from types import SimpleNamespace
    import logging

    expected_timing = f"processing first.jpg ({3.0 / jobs:.2f}s)"
    clock = [0.0]
    monkeypatch.setattr(parallel, "time", SimpleNamespace(perf_counter=lambda: clock[0]))
    caplog.set_level(logging.INFO, logger="lenzcontext.batch")

    def run(name):
        clock[0] = 103.0
        if fails:
            raise LLMError("analysis failed")

    pipeline = StubPipeline(analysis, run)
    original_prepare = pipeline.prepare

    def prepare(path):
        clock[0] = 100.0
        return original_prepare(path)

    pipeline.prepare = prepare
    original_wait = parallel.wait

    def delayed_collection(*args, **kwargs):
        result = original_wait(*args, **kwargs)
        assert expected_timing in caplog.text
        clock[0] = 999.0
        return result

    monkeypatch.setattr(parallel, "wait", delayed_collection)

    def write(image):
        clock[0] = 2000.0

    result = process_batch(["first.jpg", "broken.jpg", "skip.png"], pipeline, write, jobs=jobs)
    timings = [r.getMessage() for r in caplog.records if r.getMessage().startswith("processing ")]
    assert timings == [expected_timing]
    assert len(result.images) == (0 if fails else 1)
