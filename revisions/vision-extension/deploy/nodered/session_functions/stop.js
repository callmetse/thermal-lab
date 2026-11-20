function saveSession(a){try{const fs=global.get('fs');const base=global.get('audit_path')||'';const p=base.replace(/[^/]+$/,'session.json');if(fs&&p)fs.writeFileSync(p,JSON.stringify(a||null));}catch(e){}}
const project="abc20261102",root="labs/abc20261102/v1",defaultSampleFPS=12,captureFPS=30;

const active=global.get('active');
if(!active){msg.payload={status:'idle'};return [msg,null];}
const cmd={...active,op:'stop',issued_ms:Date.now(),generation:active.generation};global.set('active',{...active,state:'stopping',state_changed_ms:Date.now()});saveSession(global.get('active'));
 for(const k of ['observation','llm','preview','preview_id','preview_meta','preview_frame_id','llm_id','run_error'])global.set(k,null);global.set('preview_cache',{});global.set('observation_times',[]);global.set('retired_observation_instances',{});global.set('state_dirty',true);
msg.payload={status:'stopping',run_id:cmd.run_id,generation:cmd.generation};return [msg,{topic:root+'/commands',payload:JSON.stringify(cmd)}];
