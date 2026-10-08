"""Run checkout tests against this source with the installed runtime available."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile

root = Path(__file__).resolve().parents[1]
environment = dict(os.environ, PYTHONPATH=str(root / 'src'), PYTHONDONTWRITEBYTECODE='1', PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
# Use the physical system temp directory, without relaxing explicit link checks.
# Hosted Windows uses 8.3 profile aliases; macOS /var aliases /private/var.
temporary = str(Path(tempfile.gettempdir()).resolve())
environment.update(TMPDIR=temporary, TEMP=temporary, TMP=temporary)
raise SystemExit(subprocess.call([sys.executable, '-B', '-X', 'utf8', '-m', 'unittest', 'discover', '-s', 'tests', '-v'], cwd=root, env=environment))
