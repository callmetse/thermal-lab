"""Per-session rectangle selection and directional finite-line crossing events.

Coordinates are normalized to source image width/height. Rectangle membership
uses a person's bounding-box center. Line mode keeps context tracks visible and
emits events on tracked center crossings; it does not claim physical identity.
"""
import math


def settings(run):
    low, high = run.get('confidence_min', .25), run.get('confidence_max', 1.)
    if any(type(v) not in (int, float) or not math.isfinite(v) for v in (low, high)):
        raise ValueError('Confidence bounds must be finite numbers')
    if not 0 <= low <= high <= 1:
        raise ValueError('Confidence must satisfy 0 <= min <= max <= 1')
    region = run.get('recognition_region') or {'mode': 'full'}
    if not isinstance(region, dict) or region.get('mode') not in ('full', 'rectangle', 'line'):
        raise ValueError('Invalid recognition region')
    mode = region['mode']
    if mode != 'full':
        points = region.get('points')
        if not isinstance(points, list) or len(points) != 2:
            raise ValueError('Region needs exactly two endpoints')
        for point in points:
            if not isinstance(point, list) or len(point) != 2 or any(
                type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1 for v in point):
                raise ValueError('Region points must be normalized to [0,1]')
        (x1,y1),(x2,y2)=points
        if mode == 'rectangle' and (x1 >= x2 or y1 >= y2 or min(x2-x1,y2-y1)<.01):
            raise ValueError('Rectangle must have positive width and height >=1%')
        if mode == 'line' and math.hypot(x2-x1,y2-y1)<.02:
            raise ValueError('Line is too short')
        if mode == 'line' and region.get('direction', 'both') not in ('both','a_to_b','b_to_a'):
            raise ValueError('Invalid crossing direction')
    return float(low), float(high), region


class RecognitionRegion:
    def __init__(self, run):
        self.low,self.high,self.region=settings(run)
        self.history={}
        self.counts={'a_to_b':0,'b_to_a':0,'total':0}

    def apply(self, tracks, size, timestamp):
        w,h=size
        if w <= 0 or h <= 0:raise ValueError('Invalid image size')
        selected=[];events=[]
        self.history={key:v for key,v in self.history.items() if timestamp-v['seen']<=2.0}
        for t in tracks:
            score=t.get('confidence')
            if score is None or not self.low <= score <= self.high:
                continue
            x1,y1,x2,y2=t['bbox'];point=((x1+x2)/(2*w),(y1+y2)/(2*h))
            mode=self.region['mode']
            if mode == 'rectangle':
                (a,b),(c,d)=self.region['points']
                if not (a<=point[0]<=c and b<=point[1]<=d):continue
            selected.append(t)
            if mode != 'line':continue
            a,b=self.region['points'];dx=b[0]-a[0];dy=b[1]-a[1]
            distance=(dx*(point[1]-a[1])-dy*(point[0]-a[0]))/math.hypot(dx,dy)
            # A is the negative signed side, B the positive side. The two line
            # endpoints determine the arrow/orientation; A/B are sides, not ends.
            side=1 if distance>.008 else -1 if distance<-.008 else 0
            key=t['track_id'];old=self.history.get(key)
            if side == 0:
                if old:old['seen']=timestamp
                continue
            crossing=False
            if old and old['side']!=side and timestamp>old['seen'] and timestamp-old['seen']<=1.0:
                p=old['point'];travel=(point[0]-p[0],point[1]-p[1])
                denom=dx*travel[1]-dy*travel[0]
                if abs(denom)>1e-9:
                    u=((p[0]-a[0])*travel[1]-(p[1]-a[1])*travel[0])/denom
                    crossing=0<=u<=1
                direction='a_to_b' if side>0 else 'b_to_a'
                if crossing and timestamp-old.get('last_event',-1e9)>=1.0:
                    accepted=self.region.get('direction','both') in ('both',direction)
                    if accepted:
                        self.counts[direction]+=1;self.counts['total']+=1
                        events.append({'kind':'line_crossing','track_id':key,'direction':direction,
                                       'source_pts_s':timestamp,'confidence':score,
                                       'center_normalized':point,'identity_scope':'ephemeral_track'})
                    last=timestamp
                else:last=old.get('last_event',-1e9)
            else:last=old.get('last_event',-1e9) if old else -1e9
            self.history[key]={'side':side,'point':point,'seen':timestamp,'last_event':last}
        return selected,events,dict(self.counts)
