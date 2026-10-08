"""Run checkout tests against this source with the installed runtime available."""
import os
from pathlib import Path
import subprocess
import sys

root = Path(__file__).resolve().parents[1]
environment = dict(os.environ, PYTHONPATH=str(root / 'src'), PYTHONDONTWRITEBYTECODE='1')
raise SystemExit(subprocess.call([sys.executable, '-B', '-m', 'unittest', 'discover', '-s', 'tests', '-v'], cwd=root, env=environment))
