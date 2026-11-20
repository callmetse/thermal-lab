"""Prepare a new GPU venv and independent Ollama runtime; no old services stopped."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
if ROOT != Path('/root/abc20261102'):
    raise SystemExit('Only run from the staged independent GPU project')
for name in ['runtime', 'downloads', 'state', 'results']:
    (ROOT / name).mkdir(exist_ok=True)
venv = ROOT / '.venv'
if not venv.exists():
    # Read-only CUDA packages from the platform base image, never an old project venv.
    subprocess.run([sys.executable, '-m', 'venv', '--system-site-packages', str(venv)], check=True)
python = str(venv / 'bin/python')
subprocess.run([python, '-m', 'pip', 'install', '-r', str(ROOT / 'requirements-runtime.txt')], check=True)
with (ROOT / 'results/environment-freeze.txt').open('w') as f:
    subprocess.run([python, '-m', 'pip', 'freeze'], stdout=f, check=True)
archive = ROOT / 'downloads/ollama-v0.6.8-amd64.tgz'
url = 'https://github.com/ollama/ollama/releases/download/v0.6.8/ollama-linux-amd64.tgz'
if not archive.exists():
    subprocess.run(['curl', '--fail', '--location', '--retry', '2', '--connect-timeout', '15',
                    '--output', str(archive), url], check=True)
subprocess.run(['tar', '-xzf', str(archive), '-C', str(ROOT / 'runtime')], check=True)
env = os.environ.copy()
env.update(OLLAMA_HOST='127.0.0.1:28434', OLLAMA_MODELS=str(ROOT / 'state/ollama-models'),
           OLLAMA_NUM_PARALLEL='1', OLLAMA_MAX_LOADED_MODELS='1')
log = (ROOT / 'results/ollama.log').open('ab')
p = subprocess.Popen([str(ROOT / 'runtime/bin/ollama'), 'serve'], cwd=ROOT,
                     env=env, stdout=log, stderr=log, start_new_session=True)
(ROOT / 'state/ollama.pid').write_text(str(p.pid))
for _ in range(60):
    if p.poll() is not None:
        raise RuntimeError('Independent Ollama exited; inspect only this project ollama.log')
    try:
        with urllib.request.urlopen('http://127.0.0.1:28434/api/version', timeout=2) as r:
            print('Ollama ready', r.read().decode(), flush=True)
        break
    except Exception:
        time.sleep(1)
else:
    raise RuntimeError('Independent Ollama did not become ready')
subprocess.run([str(ROOT / 'runtime/bin/ollama'), 'pull', 'qwen2.5:7b'], env=env, check=True)
print(json.dumps({'status': 'gpu_runtime_ready', 'model': 'qwen2.5:7b',
                  'venv': str(venv), 'ollama_port': 28434}), flush=True)
