function saveSession(a){try{const fs=global.get('fs');const base=global.get('audit_path')||'';const p=base.replace(/[^/]+$/,'session.json');if(fs&&p)fs.writeFileSync(p,JSON.stringify(a||null));}catch(e){}}
const project="abc20261102",root="labs/abc20261102/v1",defaultSampleFPS=20,captureFPS=30;
const previewTransport="pi-local";

function observationRate(){const t=global.get('perf').now(),a=(global.get('observation_times')||[]).filter(v=>v>=t-5000);global.set('observation_times',a);
 if(!a.length||t-a[a.length-1]>2000)return 0;if(a.length<2||a[a.length-1]-a[0]<500)return null;return (a.length-1)*1000/(a[a.length-1]-a[0]);}
function compactObs(o){if(!o)return null;const v={};for(const k of ['project','run_id','arm','source','source_frame_id','source_pts_s','encoded_size','jpeg_sha256','kind','producer','status','infer_ms','producer_instance_id','observation_seq','recognition_region','confidence_range','crossing_events','crossing_counts','collection_batch_id','collection_location','collection_name','source_zone_id'])if(k in o)v[k]=o[k];
 v.tracks=(o.tracks||[]).map(t=>({track_id:t.track_id,bbox:t.bbox,confidence:t.confidence,posture:{state:t.posture?.state,reason:t.posture?.reason},action:{value:t.action?.value,reason:t.action?.reason},clothing:(t.clothing||[]).map(g=>({category:g.category,confidence:g.confidence,ambiguous_with:g.ambiguous_with})),clothing_age_s:t.clothing_age_s,clo:t.clo,met:t.met,estimate_status:t.estimate_status}));return v;}
function stateSnapshot(){const o=global.get('observation')||null,l=global.get('llm')||null,p=global.get('preview_meta')||null;let llm=null,meta=null;
 if(l){llm={};for(const k of ['project','run_id','arm','source_frame_id','model','status','draft','semantic_flags','generation_ms','error'])if(k in l)llm[k]=l[k];}
 if(p){meta={};for(const k of ['preview_id','run_id','source_frame_id','source_pts_s','encoded_size','preview_size','jpeg_sha256','preview_jpeg_sha256'])if(k in p)meta[k]=p[k];}
 return {preview_transport:previewTransport,active:global.get('active')||null,observation:compactObs(o),llm,observation_delivery_hz:observationRate(),
 observation_age_s:o?(Date.now()-global.get('obs_wall'))/1000:null,llm_age_s:l?(Date.now()-global.get('llm_wall'))/1000:null,
 preview_id:global.get('preview_id')||null,preview_meta:meta,llm_id:global.get('llm_id')||null,run_error:global.get('run_error')||null};}
const coalesced=true;

const channel=msg.topic.split('/').pop(), active=global.get('active');
if(channel==='events'){
  let sv=null;try{sv=JSON.parse(msg.payload.toString());}catch(e){}
  if(sv&&sv.project===project&&(sv.kind==='session_state'||sv.kind==='heartbeat')){
    global.set('heartbeat_wall',Date.now());
    if(sv.kind==='session_state'){
      const cur=global.get('active');
      if(cur&&sv.run_id===cur.run_id){
        if(['completed','failed','interrupted'].includes(sv.state)){
          global.set('last_session',{...cur,state:sv.state,ended_ms:Date.now(),reason:sv.reason||null});
          saveSession(null);global.set('active',null);
          for(const k of ['observation','llm','preview','preview_id','preview_meta','preview_frame_id','llm_id'])global.set(k,null);
        } else {global.set('active',{...cur,state:sv.state,state_changed_ms:Date.now()});saveSession(global.get('active'));}
      } else if(!cur&&sv.state==='running'&&sv.session){
        const adopted={...sv.session,state:'running',state_changed_ms:Date.now()};
        global.set('active',adopted);saveSession(adopted);
      }
    }
    global.set('state_dirty',true);
    return null;
  }
}let v,duplicate=false,outOfOrder=false;
const wireBytes=Buffer.isBuffer(msg.payload)?msg.payload.length:Buffer.byteLength(String(msg.payload));
const compressed=channel==='observations'&&Buffer.isBuffer(msg.payload)&&msg.payload[0]===31&&msg.payload[1]===139;
try {
 if(channel==='preview_meta'){
  v=JSON.parse(msg.payload.toString());if(v.project!==project)return null;
  if(active&&v.run_id===active.run_id){const previous=global.get('preview_frame_id');
   if(previous===null||previous===undefined||v.source_frame_id>=previous){global.set('preview_meta',v);global.set('preview_frame_id',v.source_frame_id);global.set('preview_id',v.run_id+':'+v.source_frame_id);global.set('state_dirty',true);}}
 }else if(channel==='preview') {const n=msg.payload.readUInt32BE(0);if(n>65536||n+4>=msg.payload.length)return null;v=JSON.parse(msg.payload.subarray(4,4+n).toString());
  if(v.project!==project)return null;
  if(active&&v.run_id===active.run_id){
   const id=v.run_id+':'+v.source_frame_id;v.preview_id=id;
   const cache=global.get('preview_cache')||{};cache[id]={meta:v,packet:msg.payload,jpeg:msg.payload.subarray(4+n)};
   const keys=Object.keys(cache);for(const k of keys.slice(0,Math.max(0,keys.length-24)))delete cache[k];global.set('preview_cache',cache);
   const previous=global.get('preview_frame_id');
   if(previous===null||previous===undefined||v.source_frame_id>=previous){global.set('preview',msg.payload.subarray(4+n));global.set('preview_meta',v);global.set('preview_frame_id',v.source_frame_id);global.set('preview_id',id);}
  }
 } else {let raw=msg.payload;if(compressed)raw=global.get('zlib').gunzipSync(raw,{maxOutputLength:8*1024*1024});
  v=JSON.parse(raw.toString());if(v.project!==project)return null;
  if(channel==='observations'){
   const identity=v.producer_instance_id&&v.observation_seq!=null?[v.run_id,v.producer_instance_id,v.observation_seq].join(':'):[v.run_id,v.producer,v.source_frame_id,v.jpeg_sha256].join(':');
   const seen=global.get('result_seen')||{};duplicate=Object.prototype.hasOwnProperty.call(seen,identity);
   if(!duplicate){seen[identity]=true;const keys=Object.keys(seen);for(const key of keys.slice(0,Math.max(0,keys.length-2048)))delete seen[key];global.set('result_seen',seen);}
  }
  if(active&&v.run_id===active.run_id){
   if(channel==='observations'&&!duplicate){const previous=global.get('observation');
    const instance=v.producer_instance_id||('legacy-'+v.producer),previousInstance=previous&&(previous.producer_instance_id||('legacy-'+previous.producer));
    const retired=global.get('retired_observation_instances')||{};
    outOfOrder=Object.prototype.hasOwnProperty.call(retired,instance)||!!previous&&previousInstance===instance&&v.source_frame_id<previous.source_frame_id;
    if(!outOfOrder&&previousInstance&&previousInstance!==instance){retired[previousInstance]=true;global.set('retired_observation_instances',retired);}
    const times=(global.get('observation_times')||[]).filter(t=>t>=global.get('perf').now()-5000);times.push(global.get('perf').now());global.set('observation_times',times);
    if(!outOfOrder){global.set('observation',v);global.set('obs_wall',Date.now());global.set('state_dirty',true);}}
   if(channel==='llm'){global.set('llm',v);global.set('llm_wall',Date.now());global.set('llm_id',v.run_id+':'+v.source_frame_id);}
   if(channel==='events'&&v.kind==='failed')global.set('run_error',v.error||'实验进程失败，请查看本次日志');
   if(channel==='llm'||channel==='commands'||(channel==='events'&&v.kind==='failed'))global.set('state_dirty',true);
  }
 }
 const received=global.get('perf').now();const id=v.run_id+':'+v.source_frame_id;
 const unlimited=active&&active.unlimited;
 const lastLog=global.get('unlimited_last_log')||0;
 const keepLog=!unlimited||channel==='commands'||channel==='events'||channel==='llm'||(v.crossing_events||[]).length||received-lastLog>=5000;
 if(keepLog&&channel==='observations')global.set('unlimited_last_log',received);
 if(channel==='preview'||channel==='llm'){const times=global.get('ack_times')||{};times[(channel==='preview_meta'?'preview':channel)+':'+id]=received;
  const keys=Object.keys(times);for(const k of keys.slice(0,Math.max(0,keys.length-256)))delete times[k];global.set('ack_times',times);}
 msg.payload=JSON.stringify({channel,console_boot_id:global.get('console_boot_id'),received_mono_ms:received,received_wall_ms:Date.now(),wire_bytes:wireBytes,compression:compressed?'gzip':'none',duplicate,out_of_order:outOfOrder,payload:v});
 const frame=channel==='preview'&&active&&v.run_id===active.run_id&&v.preview_id===global.get('preview_id')?{payload:global.get('preview_cache')[v.preview_id].packet}:null;
 return [keepLog?msg:null,coalesced?null:{payload:JSON.stringify(stateSnapshot())},frame];
}catch(e){return null;}
