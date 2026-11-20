const project="abc20261102",root="labs/abc20261102/v1";
const ttl=global.get('lease_ttl_s')||120;
const active=global.get('active');
if(!active){
  const t=global.get('perf').now(),last=global.get('query_at')||0;
  if(t-last>5000){global.set('query_at',t);
    return {topic:root+'/commands',payload:JSON.stringify({project,op:'session_query',issued_ms:Date.now()})};}
  return null;
}
const now=Date.now();
if(!active.generation)return null;
let seconds=ttl;
if(!active.unlimited){
  const remaining=Math.ceil((active.issued_ms+active.seconds*1000-now)/1000);
  if(remaining<=0)return null;                 // 到期由 per-run deadline 节点负责停
  seconds=Math.min(ttl,remaining);
}
return {topic:root+'/commands',payload:JSON.stringify({project,op:'lease',run_id:active.run_id,
  generation:active.generation,ttl_s:Math.max(1,seconds),issued_ms:now})};
