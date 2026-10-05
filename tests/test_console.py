import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize('module,args,expected,code', [
    ('lenzcontext', ['--help'], 'usage:', 0),
    ('lenzcontext', ['--version'], 'lenzcontext ', 0),
    ('lenzcontext', ['--invalid-option'], 'error:', 2),
    ('lenzcontext', ['missing.jpg', '--jobs', '0'], 'ERROR:', 2),
    ('lenzcontext', ['missing.jpg', '-V'], 'DEBUG:', 1),
    ('lenzcontext.geonames.importer', ['--invalid-option'], 'error:', 2),
    ('lenzcontext.geonames.importer', ['missing-source'], 'ERROR:', 1),
])
def test_console_output_uses_stdout(tmp_path, module, args, expected, code):
    env = {key: value for key, value in os.environ.items() if not key.startswith('LENZCONTEXT_')}
    env['LENZCONTEXT_MODEL'] = 'mock'
    env['PYTHONPATH'] = str(Path(__file__).resolve().parents[1])
    result = subprocess.run([sys.executable, '-m', module, *args], cwd=tmp_path,
                            env=env, capture_output=True, text=True)
    assert result.returncode == code
    assert expected in result.stdout
    assert result.stderr == ''
