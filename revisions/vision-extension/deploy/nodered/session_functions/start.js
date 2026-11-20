function saveSession(a){try{const fs=global.get('fs');const base=global.get('audit_path')||'';const p=base.replace(/[^/]+$/,'session.json');if(fs&&p)fs.writeFileSync(p,JSON.stringify(a||null));}catch(e){}}
const project="abc20261102",root="labs/abc20261102/v1",defaultSampleFPS=20,captureFPS=30;

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
const generation=(global.get('generation')||0)+1;global.set('generation',generation);run.generation=generation;run.lease_ttl_s=120;run.state='starting';run.state_changed_ms=Date.now();global.set('lease_ttl_s',120);global.set('active',run);saveSession(run);for(const k of ['observation','llm','preview','preview_id','preview_meta','preview_frame_id','llm_id','run_error'])global.set(k,null);global.set('preview_cache',{});global.set('observation_times',[]);global.set('retired_observation_instances',{});global.set('state_dirty',true);
msg.payload={status:'starting',state:'starting',run_id:run.run_id,generation};
return [msg,{topic:root+'/commands',payload:JSON.stringify(run)}, run.unlimited?null:{payload:run,delay:run.seconds*1000}];
