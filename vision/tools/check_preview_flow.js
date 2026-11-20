// Offline protocol checks against generated native Node-RED functions, not model accuracy.
const fs=require('fs'),assert=require('assert');
const nodes=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
const functions=nodes.filter(n=>n.type==='function');
for(const node of functions)new Function('msg','node','context','flow','global',node.func);
const call=(id,msg)=>new Function('msg','node','context','flow','global',nodes.find(n=>n.id===id).func)(msg,null,null,null,context);
const memory=new Map(),context={get:k=>memory.get(k),set:(k,v)=>memory.set(k,v)};
memory.set('active',{run_id:'cache_test',arm:'C',source:'replay'});
let clock=100;memory.set('perf',{now:()=>clock});memory.set('console_boot_id','test');memory.set('zlib',require('zlib'));
const project='abc20261102',root='labs/'+project+'/v1/runs/cache_test/preview';
function packet(id,text){const meta={project,run_id:'cache_test',source_frame_id:id,source_pts_s:id/30,
 preview_id:'cache_test:'+id,encoded_size:[640,480],preview_size:[480,360],jpeg_sha256:'input-'+id,
 observation:{project,run_id:'cache_test',source_frame_id:id,jpeg_sha256:'input-'+id,encoded_size:[640,480],tracks:[]}};
 const header=Buffer.from(JSON.stringify(meta)),length=Buffer.alloc(4);length.writeUInt32BE(header.length);
 return Buffer.concat([length,header,Buffer.from(text)]);}
const first=packet(1,'JPEG-first'),second=packet(2,'JPEG-second');
call('collect',{topic:root,payload:first});call('collect',{topic:root,payload:second});
let response=call('preview_frame',{req:{query:{v:'cache_test:1'}}});
assert(response.payload.equals(first));assert.equal(response.headers['X-Preview-Id'],'cache_test:1');
response=call('preview',{req:{query:{v:'cache_test:1'}}});assert.equal(response.payload.toString(),'JPEG-first');
response=call('preview_meta',{req:{query:{v:'cache_test:1'}}});assert.equal(response.payload.observation.source_frame_id,1);
call('collect',{topic:root,payload:first});assert.equal(memory.get('preview_id'),'cache_test:2','late older frame must not replace latest');
assert.equal(call('preview_frame',{req:{query:{v:'cache_test:999'}}}).statusCode,410);
memory.set('observation',{project,run_id:'cache_test',source_frame_id:2,tracks:[{track_id:7,bbox:[1,2,3,4],posture:{state:'standing'},action:{value:'unknown',probabilities:{other:1}},clothing:[],clo:null,met:null,visible_clo_reference:{large_debug_table:'remove-from-live-only'}}]});
memory.set('obs_wall',Date.now());memory.set('llm',{run_id:'cache_test',model:'qwen2.5:7b',draft:{summary:'test',uncertainty:'test'},model_manifest:{large:'omit'}});memory.set('llm_wall',Date.now()-5000);
response=call('state',{});assert.equal(response.payload.observation.tracks[0].track_id,7);
assert(!('visible_clo_reference' in response.payload.observation.tracks[0]));assert(!('model_manifest' in response.payload.llm));
assert(response.payload.llm_age_s>=5&&response.payload.llm_age_s<6);
assert('visible_clo_reference' in memory.get('observation').tracks[0],'raw evidence must remain intact');
const result={project,run_id:'cache_test',source_frame_id:3,producer:'cloud',producer_instance_id:'boot1',observation_seq:1,jpeg_sha256:'input-3',tracks:[],audit:{null_value:null,table:'large-table '.repeat(300)}};
const gz=require('zlib').gzipSync(Buffer.from(JSON.stringify(result)));
let collected=call('collect',{topic:'labs/'+project+'/v1/runs/cache_test/observations',payload:gz});
assert.deepEqual(memory.get('observation'),result);assert.equal(JSON.parse(collected[0].payload).compression,'gzip');
const times=memory.get('observation_times').length;
clock+=500;collected=call('collect',{topic:'labs/'+project+'/v1/runs/cache_test/observations',payload:gz});
assert.equal(JSON.parse(collected[0].payload).duplicate,true);assert.equal(memory.get('observation_times').length,times);
const older={...result,source_frame_id:2,observation_seq:2};
collected=call('collect',{topic:'labs/'+project+'/v1/runs/cache_test/observations',payload:Buffer.from(JSON.stringify(older))});
assert.equal(JSON.parse(collected[0].payload).out_of_order,true);assert.equal(memory.get('observation').source_frame_id,3);
assert.equal(call('state',{}).payload.observation_delivery_hz,2);
clock+=2100;assert.equal(call('state',{}).payload.observation_delivery_hz,0);
const restarted={...result,producer_instance_id:'boot2',observation_seq:1,source_frame_id:0};
call('collect',{topic:'labs/'+project+'/v1/runs/cache_test/observations',payload:Buffer.from(JSON.stringify(restarted))});
assert.equal(memory.get('observation').producer_instance_id,'boot2');
const retired={...result,source_frame_id:10,observation_seq:10};
collected=call('collect',{topic:'labs/'+project+'/v1/runs/cache_test/observations',payload:Buffer.from(JSON.stringify(retired))});
assert.equal(JSON.parse(collected[0].payload).out_of_order,true);assert.equal(memory.get('observation').producer_instance_id,'boot2');
for(const client of ['tablet-one','tablet-two']){
 response=call('ack',{payload:{run_id:'cache_test',kind:'preview',id:'cache_test:2',client_id:client,sampled:true,sample_max_hz:1}});
 assert.equal(response[0].payload.ok,true);assert(response[1],'each real client should have its own ACK');assert.equal(JSON.parse(response[1].payload).payload.sampled,true);
}
response=call('ack',{payload:{run_id:'cache_test',kind:'preview',id:'cache_test:2',client_id:'tablet-one'}});
assert.equal(response[0].payload.duplicate,true);assert.equal(response[1],null);
if(nodes.some(n=>n.id==='live_snapshot')){
 memory.set('state_dirty',true);const push=call('live_snapshot',{});assert(push);assert.equal(call('live_snapshot',{}),null);
 clock+=1100;assert(call('live_snapshot',{}),'idle heartbeat keeps HTTP fallback viable');
}
memory.set('active',null);assert.equal(call('preview_frame',{req:{query:{v:'cache_test:1'}}}).statusCode,410);

memory.set('crypto',require('crypto'));memory.set('active',null);
let started=call('start',{payload:{arm:'C',source:'replay',seconds:15,sample_fps:20}});
assert.equal(started[0].payload.sample_fps,20);
assert.equal(JSON.parse(started[1].payload).sample_fps,20);
memory.set('active',null);
assert.equal(call('start',{payload:{arm:'C',source:'replay',seconds:15,sample_fps:31}})[0].statusCode,400);
console.log(JSON.stringify({syntax_functions:functions.length,checks:'passed',scope:'cached same-frame identity, compact/raw evidence, gzip/plain parity, duplicate and older-result handling, server frequency, per-client sampled ACK, coalesced state heartbeat, stopped/expired frames'}));
