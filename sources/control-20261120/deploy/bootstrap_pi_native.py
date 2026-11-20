"""Prepare new Pi packages and use official NCNN export; does not open a camera."""
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
if ROOT != Path('/home/pi/abc20261102'):
    raise SystemExit('Only run from the staged independent Pi project')
venv = ROOT / '.venv'
if not venv.exists():
    subprocess.run([sys.executable, '-m', 'venv', '--system-site-packages', str(venv)], check=True)
python = str(venv / 'bin/python')
local_wheels = list((ROOT / 'downloads').glob('*.whl'))
if local_wheels:
    subprocess.run([python, '-m', 'pip', 'install', *map(str, local_wheels)], check=True)
subprocess.run([python, '-m', 'pip', 'install', '--find-links', str(ROOT / 'downloads'), 'torch==2.9.1', 'torchvision==0.24.1',
                'ncnn==1.0.20261020', 'pnnx==20261020', 'supervisor>=4.3,<5'], check=True)
subprocess.run([python, '-m', 'pip', 'install', '-r', str(ROOT / 'requirements-runtime.txt')], check=True)
subprocess.run([python, str(ROOT / 'tools/export_models.py')], check=True)
with (ROOT / 'results/environment-freeze.txt').open('w') as f:
    subprocess.run([python, '-m', 'pip', 'freeze'], stdout=f, check=True)
print('Independent Pi runtime ready; camera remains disabled', flush=True)
