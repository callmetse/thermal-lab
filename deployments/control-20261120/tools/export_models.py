"""Official NCNN conversions from exact experiment checkpoints; never trains."""
from pathlib import Path
import hashlib
import json
import os

root = Path(__file__).resolve().parent.parent
cfg = root / 'state/ultralytics'
cfg.mkdir(parents=True, exist_ok=True)
os.environ['YOLO_CONFIG_DIR'] = str(cfg)
os.environ['YOLO_OFFLINE'] = 'true'
from ultralytics import YOLO
import torch

torch.set_num_threads(4)
models = []
for name, size in [('yolo11n', 416), ('yolo11n-pose', 640), ('clothing_best', 512)]:
    source = root / 'assets' / (name + '.pt')
    output = Path(YOLO(str(source)).export(format='ncnn', imgsz=size))
    models.append({'name': name, 'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
                   'imgsz': size, 'files': {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                          for p in output.iterdir() if p.suffix in ('.yaml', '.param', '.bin')}})
(root / 'assets/export_manifest.json').write_text(json.dumps({'torch': torch.__version__, 'format': 'ncnn', 'models': models}, indent=2) + '\n')
