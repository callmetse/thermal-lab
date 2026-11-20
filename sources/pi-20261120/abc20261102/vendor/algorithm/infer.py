"""Copied garment/person association helpers; no old LLM or CLI entry points."""

def associate(garment,people,min_coverage=0.7,margin=0.15):
    x1,y1,x2,y2=garment
    area=max(0,x2-x1)*max(0,y2-y1)
    if area<=0:return None,'invalid_garment_box'
    scores=[]
    for i,p in enumerate(people):
        a,b,c,d=p
        inside=a<=(x1+x2)/2<=c and b<=(y1+y2)/2<=d
        overlap=max(0,min(x2,c)-max(x1,a))*max(0,min(y2,d)-max(y1,b))/area
        scores.append((overlap if inside else 0,i))
    scores.sort(reverse=True)
    if not scores or scores[0][0]<min_coverage:return None,'no_supported_person'
    if len(scores)>1 and scores[0][0]-scores[1][0]<margin:return None,'overlapping_people_ambiguous'
    return scores[0][1],None

def mark_class_ambiguities(items):
    for i,a in enumerate(items):
        for b in items[i+1:]:
            if a['category']==b['category']:continue
            x1,y1,x2,y2=a['bbox_xyxy'];u1,v1,u2,v2=b['bbox_xyxy']
            inter=max(0,min(x2,u2)-max(x1,u1))*max(0,min(y2,v2)-max(y1,v1))
            union=max(0,x2-x1)*max(0,y2-y1)+max(0,u2-u1)*max(0,v2-v1)-inter
            if union>0 and inter/union>=0.85:
                a.setdefault('ambiguous_with',[]).append(b['category'])
                b.setdefault('ambiguous_with',[]).append(a['category'])
