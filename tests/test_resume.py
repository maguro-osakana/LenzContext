import os
from unittest.mock import Mock

import pytest
import yaml

from lenzcontext.cli import main
from lenzcontext.models import BatchResult, ExifInfo, FileInfo, ImageResult
from lenzcontext.output import IncrementalYamlWriter, InvalidOutput, path_key, read_existing_names, write_yaml


def record(name, analysis):
    return ImageResult(file=FileInfo(name=name), exif=ExifInfo(), address=None, analysis=analysis)


@pytest.mark.parametrize('jobs', [1, 4])
@pytest.mark.parametrize('recursive', [False, True])
def test_resume_skips_existing_and_preserves_output(monkeypatch, tmp_path, make_jpeg, analysis, jobs, recursive):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('LENZCONTEXT_MODEL', 'mock')
    first = make_jpeg('A.jpg')
    second = make_jpeg('B.jpg')
    output = tmp_path / 'result.yaml'
    write_yaml(BatchResult(images=[record('./A.jpg', analysis)]), output)
    original = output.read_bytes()
    analyze = Mock(return_value=analysis)
    monkeypatch.setattr('lenzcontext.llm.openai_compatible.OpenAICompatibleAnalyzer.analyze', analyze)
    inputs = ['-R', str(tmp_path)] if recursive else [str(first), str(second)]
    assert main([*inputs, '--resume', str(output), '--jobs', str(jobs)]) == 0
    assert analyze.call_count == 1
    assert output.read_bytes().startswith(original)
    assert [r['file']['name'] for r in yaml.safe_load(output.read_text())['images']] == ['./A.jpg', str(second)]
    completed = output.read_bytes()
    assert main([*inputs, '--resume', str(output), '--jobs', str(jobs)]) == 0
    assert analyze.call_count == 1
    assert output.read_bytes() == completed


def test_path_keys(tmp_path):
    base = str(tmp_path / 'lenzcontext')
    expected = os.path.join(base, 'sample/A.jpg')
    for name in ('../lenzcontext/sample/A.jpg', 'sample/A.jpg', './sample/A.jpg', base + '//sample/A.jpg'):
        assert path_key(name, base) == expected
    assert path_key('sameple/A.jpg', base) != expected


@pytest.mark.parametrize('initial', [None, '', 'version: 1\nimages: []\n'])
def test_resume_creates_output_and_keeps_new_duplicates(monkeypatch, tmp_path, make_jpeg, analysis, initial):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('LENZCONTEXT_MODEL', 'mock')
    image = make_jpeg('A.jpg')
    output = tmp_path / 'result.yaml'
    if initial is not None:
        output.write_text(initial)
    analyze = Mock(return_value=analysis)
    monkeypatch.setattr('lenzcontext.llm.openai_compatible.OpenAICompatibleAnalyzer.analyze', analyze)
    assert main([str(image), str(image), '--resume', str(output), '--jobs', '4']) == 0
    assert analyze.call_count == 2
    assert len(yaml.safe_load(output.read_text())['images']) == 2


@pytest.mark.parametrize('contents', [
    'not YAML: [', 'version: 2\nimages: []\n', 'version: 1\nimages: null\n',
    'version: 1\nimages: []\nother: value\n', 'version: 1\nimages: []\n---\nversion: 1\n',
    'version: 1\nimages: []\n# comment\n', 'version: 1\nimages:\n- file: {name: A.jpg}\n',
])
def test_resume_invalid_output_untouched(monkeypatch, tmp_path, contents):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('LENZCONTEXT_MODEL', 'mock')
    output = tmp_path / 'result.yaml'
    output.write_text(contents)
    with pytest.raises(InvalidOutput):
        read_existing_names(output, str(tmp_path))
    assert main(['A.jpg', '--resume', str(output)]) == 2
    assert output.read_text() == contents


def test_resume_streams_records(monkeypatch, tmp_path, analysis):
    output = tmp_path / 'result.yaml'
    writer = IncrementalYamlWriter(output)
    for i in range(20):
        writer.write(record(f'{i}.jpg', analysis))
    monkeypatch.setattr(yaml, 'safe_load', Mock(side_effect=AssertionError('whole-file read')))
    names = read_existing_names(output, str(tmp_path))
    assert names == {str(tmp_path / f'{i}.jpg') for i in range(20)}


@pytest.mark.parametrize('flag', ['-o', '-a'])
def test_resume_output_modes_exclusive(flag):
    with pytest.raises(SystemExit) as error:
        main(['A.jpg', '--resume', 'result.yaml', flag, 'other.yaml'])
    assert error.value.code == 2


def test_resume_multiline_unicode_and_large_record(monkeypatch, tmp_path, analysis):
    output = tmp_path / 'result.yaml'
    analysis = analysis.model_copy(update={'description': '日本語\n- file:\n' + '説明 ' * 5000})
    writer = IncrementalYamlWriter(output)
    writer.write(record('画像\nA.jpg', analysis))
    writer.write(record('B.jpg', analysis))
    assert read_existing_names(output, str(tmp_path)) == {
        str(tmp_path / '画像\nA.jpg'), str(tmp_path / 'B.jpg')}
    assert writer.existing_names == set()


def test_resume_rejects_noncanonical_record(tmp_path, analysis):
    output = tmp_path / 'result.yaml'
    IncrementalYamlWriter(output).write(record('A.jpg', analysis))
    original = output.read_text().replace('name: A.jpg', 'name: "A.jpg"')
    output.write_text(original)
    with pytest.raises(InvalidOutput):
        read_existing_names(output, str(tmp_path))
    assert output.read_text() == original
