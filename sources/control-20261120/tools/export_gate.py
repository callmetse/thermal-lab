"""Official export API; no model training. Run inside the NEW Pi venv."""
from pathlib import Path
import os

root = Path(__file__).resolve().parent.parent
directory = root / "state/ultralytics"
directory.mkdir(parents=True, exist_ok=True)
os.environ["YOLO_CONFIG_DIR"] = str(directory)
os.environ["YOLO_OFFLINE"] = "true"
from ultralytics import YOLO
YOLO(str(root / "assets/yolo11n.pt")).export(format="ncnn", imgsz=416)
