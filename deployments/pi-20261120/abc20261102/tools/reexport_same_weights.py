from pathlib import Path
import os,hashlib,json,sys,time
root=Path('/home/pi/abc20261102');os.environ['YOLO_CONFIG_DIR']=str(root/'state/ultralytics');os.environ['YOLO_OFFLINE']='true'
from ultralytics import YOLO
import torch,ncnn,pnnx
torch.set_num_threads(4)
rows=[]
for name,size in [('yolo11n-pose',640),('clothing_best',512)]:
 checkpoint=root/'assets'/(name+'.pt');start=time.monotonic()
 output=Path(YOLO(str(checkpoint)).export(format='ncnn',imgsz=size))
 rows.append({'name':name,'source_sha256':hashlib.sha256(checkpoint.read_bytes()).hexdigest(),'imgsz':size,'export_s':time.monotonic()-start,'files':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in output.iterdir() if p.suffix in ['.bin','.param','.yaml']}})
(root/'results/ncnn-reexport.json').write_text(json.dumps({'torch':torch.__version__,'models':rows},indent=2)+'\n')
print('Fresh NCNN exports from the exact copied .pt checkpoints complete',flush=True)
