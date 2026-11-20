"""GPU-side, isolated ONE-EPOCH pipeline smoke. Labels are teacher proposals.

The replay clip is shared across train/val; this deliberately blocks release.
No accuracy or generalization claim can be made from this run.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import time


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--package', type=Path, required=True)
    args = ap.parse_args()
    out = args.out.resolve(); out.mkdir(parents=True, exist_ok=True)
    os.environ['YOLO_CONFIG_DIR'] = str(out/'yolo_settings')
    os.environ['WANDB_MODE'] = 'disabled'
    import cv2
    import torch
    from ultralytics import YOLO
    root = Path('/root/abc20261102')
    source = root/'data/replay.mp4'
    teacher_path = root/'assets/yolo11n-pose.pt'
    student_path = root/'assets/yolo11n.pt'
    student = YOLO(str(student_path))
    teacher = YOLO(str(teacher_path))
    entries = []
    dataset = out/'dataset'
    for split in ('train', 'val'):
        (dataset/'images'/split).mkdir(parents=True, exist_ok=True)
        (dataset/'labels'/split).mkdir(parents=True, exist_ok=True)
    cap = cv2.VideoCapture(str(source))
    frames = [15, 45, 75, 105, 135, 165, 225, 255, 285, 315, 345, 375]
    for i, fid in enumerate(frames):
        cap.set(cv2.CAP_PROP_POS_FRAMES, fid)
        ok, im = cap.read()
        if not ok:
            raise RuntimeError('Replay read failed at '+str(fid))
        split = 'train' if i < 8 else 'val'
        name = 'replay_%03d' % fid
        image = dataset/'images'/split/(name+'.jpg')
        cv2.imwrite(str(image), im)
        prediction = teacher.predict(im, imgsz=640, conf=.25, device=0, verbose=False)[0]
        boxes = prediction.boxes.xywhn.cpu().tolist()
        (dataset/'labels'/split/(name+'.txt')).write_text(
            ''.join('0 '+' '.join('%.7f'%v for v in b)+'\n' for b in boxes))
        entries.append({'image':str(image.relative_to(dataset)), 'sha256':sha(image),
                        'source_sha256':sha(source), 'source_frame_id':fid,
                        'split':split, 'boxes':len(boxes), 'label_status':'teacher_proposed_not_human_verified',
                        'teacher':'yolo11n-pose', 'teacher_sha256':sha(teacher_path)})
    cap.release()
    # Include the real edge-collected samples in the pipeline; propose labels
    # with the same teacher, preserving sample_id/hash and original provenance.
    package_records = [json.loads(s) for s in (args.package/'manifest.jsonl').read_text().splitlines()]
    for rec in package_records:
        src = args.package/'samples'/(rec['sample_id']+'.jpg')
        assert sha(src) == rec['media_sha256']
        dest = dataset/'images/train'/('edge_'+rec['sample_id']+'.jpg')
        shutil.copy2(src,dest)
        im = cv2.imread(str(dest)); assert im is not None
        pred = teacher.predict(im,imgsz=640,conf=.25,device=0,verbose=False)[0]
        boxes = pred.boxes.xywhn.cpu().tolist()
        (dataset/'labels/train'/(dest.stem+'.txt')).write_text(
            ''.join('0 '+' '.join('%.7f'%v for v in b)+'\n' for b in boxes))
        entries.append({'image':str(dest.relative_to(dataset)), 'sha256':sha(dest),
                        'source_sample_id':rec['sample_id'], 'source_run_id':rec.get('run_id'),
                        'split':'train','boxes':len(boxes),
                        'label_status':'teacher_proposed_not_human_verified',
                        'teacher_sha256':sha(teacher_path)})
    names = student.names
    (dataset/'data.yaml').write_text('path: '+str(dataset)+'\ntrain: images/train\nval: images/val\nnames:\n'+
        ''.join('  %s: %s\n'%(k,json.dumps(v)) for k,v in names.items()))
    manifest = {'purpose':'pipeline_smoke_only', 'entries':entries,
                'human_verified':False,'independent_test_set':False,
                'leakage_warning':'train and val replay frames share the same source recording',
                'parent_sha256':sha(student_path), 'teacher_sha256':sha(teacher_path),
                'class_map':names,'eligible_for_production':False}
    (out/'dataset_manifest.json').write_text(json.dumps(manifest,indent=2))
    print(json.dumps({'stage':'dataset_ready','images':len(entries),'positive_images':sum(e['boxes']>0 for e in entries)}),flush=True)
    started=time.time()
    student.train(data=str(dataset/'data.yaml'),epochs=1,imgsz=416,batch=4,
                  device=0,workers=0,cache=False,amp=False,plots=True,
                  project=str(out),name='training',exist_ok=False,seed=42,
                  deterministic=True,freeze=10,save=True,verbose=False)
    best=out/'training/weights/best.pt'
    assert best.exists()
    candidate=YOLO(str(best))
    comparisons=[]
    for image in sorted((dataset/'images/val').glob('*.jpg')):
        pred=candidate.predict(str(image),imgsz=416,classes=[0],conf=.25,device=0,verbose=False)[0]
        comparisons.append({'name':image.name,'sha256':sha(image),
                            'boxes':pred.boxes.xyxy.cpu().tolist(),
                            'confidence':pred.boxes.conf.cpu().tolist()})
    release={'schema_version':'candidate_smoke_v1','candidate_sha256':sha(best),
             'parent_sha256':sha(student_path),'trained_epochs':1,
             'elapsed_s':round(time.time()-started,3),'torch':torch.__version__,
             'eligible_for_production':False,
             'blocked_reasons':['unreviewed_teacher_labels','no_independent_test_set','one_epoch_smoke_only'],
             'reference_predictions':comparisons}
    (out/'candidate_manifest.json').write_text(json.dumps(release,indent=2))
    print(json.dumps({'stage':'trained_candidate','path':str(best),'sha256':sha(best),
                      'elapsed_s':release['elapsed_s'],'production_release':'BLOCKED'}),flush=True)


if __name__=='__main__':
    main()
