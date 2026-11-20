"""Generate portable Node-RED flows, using built-in HTTP/MQTT/file nodes only."""
import json
from pathlib import Path
import sys
import argparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from lab.common import canonical_hash, config

c = config()
parser = argparse.ArgumentParser()
parser.add_argument("--native", action="store_true")
args = parser.parse_args()
topic = c["topic_root"]
flows = [{"id": "lab", "type": "tab", "label": "独立 A/B/C · 7B"},
         {"id": "live_ws", "type": "websocket-listener", "path": "/live", "wholemsg": "false"},
         {"id": "video_ws", "type": "websocket-listener", "path": "/live-video", "wholemsg": "false"},
         {"id": "broker", "type": "mqtt-broker", "name": "Independent lab broker",
          "broker": "127.0.0.1" if args.native else "broker", "port": "28883" if args.native else "1883", "clientid": c["project"] + "-console",
          "usetls": False, "protocolVersion": "4", "keepalive": "30", "cleansession": True}]


def node(id, type, **kw):
    # Node-RED uses coordinates to distinguish runtime nodes from config nodes.
    flows.append({"id": id, "type": type, "z": "lab", "x": 250, "y": 40 * len(flows), **kw})


def endpoint(name, method, url, code, outputs=1, wires=None):
    node(name + "_in", "http in", name=url, url=url, method=method, upload=False, wires=[[name]])
    node(name, "function", name=name, func=code, outputs=outputs, noerr=0,
         wires=wires or [["http_response"]])


node("http_response", "http response", statusCode="", headers={})
page = (ROOT / "deploy/nodered/page.html").read_text(encoding="utf-8")
endpoint("page", "get", "/lab", "msg.headers={'Content-Type':'text/html; charset=utf-8'};msg.payload=" + json.dumps(page) + ";return msg;")
snapshot = "const previewTransport=" + json.dumps("pi-local" if c.get("local_preview", False) else "cloud") + ";\n" + """
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
"""
endpoint("state", "get", "/api/state", snapshot + "msg.headers={'Cache-Control':'no-store'};msg.payload=stateSnapshot();return msg;")
endpoint("raw_state", "get", "/api/raw-state", "msg.headers={'Cache-Control':'no-store'};msg.payload={active:global.get('active')||null,observation:global.get('observation')||null,llm:global.get('llm')||null};return msg;")
preview_lookup = """
const requested=msg.req.query.v,id=requested||global.get('preview_id'),cache=global.get('preview_cache')||{};
const entry=id&&Object.prototype.hasOwnProperty.call(cache,id)?cache[id]:null,active=global.get('active');
msg.headers={'Cache-Control':'no-store'};
if(!entry||!active||entry.meta.run_id!==active.run_id){msg.statusCode=requested?410:404;msg.headers['Content-Type']='application/json';msg.payload={error:'该帧已过期，等待最新画面'};return msg;}
msg.headers['X-Preview-Id']=id;
"""
endpoint("preview", "get", "/api/preview", preview_lookup + "msg.headers['Content-Type']='image/jpeg';msg.payload=entry.jpeg;return msg;")
endpoint("preview_meta", "get", "/api/preview-meta", preview_lookup + "msg.headers['Content-Type']='application/json';msg.payload=entry.meta;return msg;")
endpoint("preview_frame", "get", "/api/preview-frame", preview_lookup + "msg.headers['Content-Type']='application/octet-stream';msg.payload=entry.packet;return msg;")
prefix = f"const project={json.dumps(c['project'])},root={json.dumps(topic)},defaultSampleFPS={c['sample_fps']},captureFPS={c['capture_fps']};\n"
endpoint("start", "post", "/api/start", prefix + """
const v=msg.payload||{},sampleFps=v.sample_fps??defaultSampleFPS;
if(!Number.isFinite(sampleFps)||sampleFps<1||sampleFps>captureFPS){msg.statusCode=400;msg.payload={error:'送检频率需在1与采集帧率之间'};return [msg,null,null];}
if(global.get('active')){msg.statusCode=409;msg.payload={error:'已有实验运行，请先停止'};return [msg,null,null];}
if(!['A','B','C'].includes(v.arm)||!['replay','camera'].includes(v.source)||typeof (v.unlimited??false)!=='boolean'||!Number.isFinite(v.seconds)||(v.unlimited?(v.seconds!==0||v.source!=='camera'):(v.seconds<1||v.seconds>3600))){msg.statusCode=400;msg.payload={error:'无效参数'};return [msg,null,null];}
if(v.upload_id!=null&&(!/^[a-f0-9]{32}$/.test(v.upload_id)||v.source!=='replay')){msg.statusCode=400;msg.payload={error:'上传视频编号无效'};return [msg,null,null];}
const location=v.collection_location??'',batchName=v.collection_name??'';
if(typeof location!=='string'||typeof batchName!=='string'||location.length>160||batchName.length>160){msg.statusCode=400;msg.payload={error:'采集地点/批次名称最多160字'};return [msg,null,null];}
const lo=v.confidence_min??0.25,hi=v.confidence_max??1,region=v.recognition_region??{mode:'full'};
if(!Number.isFinite(lo)||!Number.isFinite(hi)||lo<0||lo>hi||hi>1||!region||!['full','rectangle','line'].includes(region.mode)){msg.statusCode=400;msg.payload={error:'置信度范围或识别区域无效'};return [msg,null,null];}
if(region.mode!=='full'){
const pts=region.points;
if(!Array.isArray(pts)||pts.length!==2||pts.some(p=>!Array.isArray(p)||p.length!==2||p.some(x=>!Number.isFinite(x)||x<0||x>1))){msg.statusCode=400;msg.payload={error:'区域坐标需在0到1之间'};return [msg,null,null];}
const [[x1,y1],[x2,y2]]=pts;
if(region.mode==='rectangle'&&(x2-x1<.01||y2-y1<.01)||region.mode==='line'&&(Math.hypot(x2-x1,y2-y1)<.02||!['both','a_to_b','b_to_a'].includes(region.direction??'both'))){msg.statusCode=400;msg.payload={error:'区域尺寸或跨线方向无效'};return [msg,null,null];}
}
const run={project,op:'start',run_id:global.get('crypto').randomUUID(),arm:v.arm,source:v.source,sample_fps:sampleFps,seconds:v.seconds,unlimited:!!v.unlimited,confidence_min:lo,confidence_max:hi,recognition_region:region,collection_location:location.trim()||'未声明',collection_name:batchName.trim()||'视觉与环境同步采集',source_zone_id:'lab',issued_ms:Date.now()};run.collection_batch_id=run.run_id;if(v.upload_id){run.upload_id=v.upload_id;run.ui_channel='D';}
global.set('active',run);for(const k of ['observation','llm','preview','preview_id','preview_meta','preview_frame_id','llm_id','run_error'])global.set(k,null);global.set('preview_cache',{});global.set('observation_times',[]);global.set('retired_observation_instances',{});global.set('state_dirty',true);
msg.payload=run;
return [msg,{topic:root+'/commands',payload:JSON.stringify(run)}, run.unlimited?null:{payload:run,delay:run.seconds*1000}];
""", outputs=3, wires=[["http_response"], ["mqtt_out"], ["timer"]])
stop = prefix + """
const active=global.get('active');
if(!active){msg.payload={status:'idle'};return [msg,null];}
const cmd={...active,op:'stop',issued_ms:Date.now()};global.set('active',null);
 for(const k of ['observation','llm','preview','preview_id','preview_meta','preview_frame_id','llm_id','run_error'])global.set(k,null);global.set('preview_cache',{});global.set('observation_times',[]);global.set('retired_observation_instances',{});global.set('state_dirty',true);
msg.payload={status:'stopping',run_id:cmd.run_id};return [msg,{topic:root+'/commands',payload:JSON.stringify(cmd)}];
"""
endpoint("stop", "post", "/api/stop", stop, outputs=2, wires=[["http_response"], ["mqtt_out"]])
node("timer", "delay", name="Per-run deadline", pauseType="delayv", timeout="60", timeoutUnits="seconds", rate="1", nbRateUnits="1", rateUnits="second", randomFirst="1", randomLast="5", randomUnits="seconds", drop=False, allowrate=False, wires=[["auto_stop"]])
node("auto_stop", "function", name="Stop only matching run", func=prefix + """
const a=global.get('active');if(!a||a.run_id!==msg.payload.run_id)return null;
global.set('active',null);for(const k of ['observation','llm','preview','preview_id','preview_meta','preview_frame_id','llm_id','run_error'])global.set(k,null);global.set('preview_cache',{});global.set('observation_times',[]);global.set('retired_observation_instances',{});global.set('state_dirty',true);
return {topic:root+'/commands',payload:JSON.stringify({...a,op:'stop',issued_ms:Date.now()})};
""", outputs=1, wires=[["mqtt_out"]])
node("mqtt_out", "mqtt out", name="Only new broker", topic="", qos="1", retain="false", broker="broker", wires=[])
node("lease_tick", "inject", name="Reannounce active session for reconnects", props=[{"p": "payload"}],
     repeat="2", crontab="", once=False, onceDelay="0.1", topic="", payload="", payloadType="date",
     wires=[["lease_announce"]])
node("lease_announce", "function", name="Current session with remaining lease", func=prefix + """
const active=global.get('active');if(!active)return null;
const now=Date.now(),remaining=Math.ceil((active.issued_ms+active.seconds*1000-now)/1000);
if(remaining<=0)return null;
return {topic:root+'/commands',payload:JSON.stringify({...active,issued_ms:now,seconds:remaining,lease_refresh:true})};
""", outputs=1, wires=[["mqtt_out"]])
coalesced = c.get('live_state_hz', 0) > 0
collector = prefix + snapshot + "const coalesced=" + json.dumps(coalesced) + ";\n" + """
const channel=msg.topic.split('/').pop(), active=global.get('active');let v,duplicate=false,outOfOrder=false;
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
"""
for channel in ("observations", "events", "llm", "preview", "preview_meta"):
    node("mqtt_" + channel, "mqtt in", name=channel, topic=topic + "/runs/+/" + channel,
         qos="1" if channel=='observations' else "0", datatype="buffer", broker="broker", nl=False, rap=False, rh=0, wires=[["collect"]])
# Log commands using the same console monotonic clock as observations and ACKs.
node("mqtt_commands", "mqtt in", name="Command timestamps", topic=topic + "/commands", qos="1", datatype="buffer", broker="broker", nl=False, rap=False, rh=0, wires=[["collect"]])
node("collect", "function", name="Isolated run filtering + metrics", func=collector, outputs=3, wires=[["log"], ["live_out"], ["video_out"]])
node("live_out", "websocket out", name="Native Node-RED live push", server="live_ws", client="", wires=[])
node("video_out", "websocket out", name="Native binary preview push", server="video_ws", client="", wires=[])
if coalesced:
    node('live_tick', 'inject', name='Coalesce UI state; keep newest only', props=[{'p':'payload'}], repeat=str(1/c['live_state_hz']), crontab='', once=False, onceDelay='0.1', topic='', payload='', payloadType='date', wires=[['live_snapshot']])
    node('live_snapshot', 'function', name='Latest state with idle heartbeat', func=snapshot+"const t=global.get('perf').now();if(!global.get('state_dirty')&&t-(global.get('last_live_at')||0)<1000)return null;global.set('state_dirty',false);global.set('last_live_at',t);return {payload:JSON.stringify(stateSnapshot())};", outputs=1, wires=[['live_out']])
endpoint("ack", "post", "/api/ack", """
const v=msg.payload||{}, a=global.get('active'), times=global.get('ack_times')||{};
const received=times[v.kind+':'+v.id];
if(!a||a.run_id!==v.run_id||!['preview','llm'].includes(v.kind)||typeof v.id!=='string'||!v.id.startsWith(v.run_id+':')||received===undefined){msg.statusCode=409;msg.payload={error:'旧结果或无对应的服务器记录'};return [msg,null];}
const clientId=typeof v.client_id==='string'?v.client_id.slice(0,100):'legacy';
const identity=[v.kind,v.id,clientId].join(':'),seen=global.get('ack_seen')||{};
if(Object.prototype.hasOwnProperty.call(seen,identity)){msg.payload={ok:true,duplicate:true};return [msg,null];}
seen[identity]=true;const keys=Object.keys(seen);for(const key of keys.slice(0,Math.max(0,keys.length-1024)))delete seen[key];global.set('ack_seen',seen);
const row={channel:'display_ack',console_boot_id:global.get('console_boot_id'),received_mono_ms:global.get('perf').now(),payload:{...v,console_to_ack_ms:global.get('perf').now()-received}};
msg.payload={ok:true};return [msg,{payload:JSON.stringify(row)}];
""", outputs=2, wires=[["http_response"], ["log"]])
node("log", "function", name="Bounded project JSONL (64 MiB x 2)", outputs=1, wires=[[]], func="""
const fs=global.get('fs'),path=global.get('audit_path');
if(!fs||!path){node.error('Bounded audit writer not configured');return null;}
try{const text=(typeof msg.payload==='string'?msg.payload:JSON.stringify(msg.payload))+'\\n';
const bytes=Buffer.byteLength(text);let size=0;try{size=fs.statSync(path).size}catch(e){if(e.code!=='ENOENT')throw e;}
if(size+bytes>64*1024*1024){try{fs.unlinkSync(path+'.1')}catch(e){if(e.code!=='ENOENT')throw e;}fs.renameSync(path,path+'.1');}
fs.mkdirSync(path.slice(0,path.lastIndexOf('/')),{recursive:true});fs.appendFileSync(path,text);return null;
}catch(e){node.error('Audit log write failed: '+e.message);return null;}
""")
output = "flows-native.json" if args.native else "flows.json"
(ROOT / "deploy/nodered" / output).write_text(json.dumps(flows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
(ROOT / "deploy/profile.sha256").write_text(canonical_hash(c) + "\n")
print(f"Generated {len(flows)} Node-RED nodes for {topic}")
