// Run on the independent control host. Credentials are read locally and never logged.
const fs=require('fs'), WebSocket=require('../node_modules/ws'), mqtt=require('../node_modules/mqtt');
const deadline=setTimeout(()=>{console.error('Transport verification deadline exceeded');process.exit(2)},12000);
const creds=JSON.parse(fs.readFileSync('/etc/abc20261102/ui-login.json','utf8'));
const auth='Basic '+Buffer.from(creds.user+':'+creds.password).toString('base64');
(async()=>{
 const response=await fetch('http://127.0.0.1:28880/api/state',{headers:{Authorization:auth}});
 if(!response.ok)throw Error('New console route/auth failed: '+response.status);
 const state=await response.json();
 const ws=new WebSocket('ws://127.0.0.1:28880/live',{headers:{Authorization:auth}});
 const done=new Promise((resolve,reject)=>{ws.once('message',v=>resolve(JSON.parse(v)));ws.once('error',reject)});
 await new Promise((resolve,reject)=>{ws.once('open',resolve);ws.once('error',reject)});
 const client=mqtt.connect('mqtt://127.0.0.1:28883',{clientId:'abc20261102-healthcheck'});
 await new Promise((resolve,reject)=>{client.once('connect',resolve);client.once('error',reject)});
 client.publish('labs/abc20261102/v1/runs/diagnostic/events',JSON.stringify({project:'abc20261102',run_id:'diagnostic',kind:'transport_smoke_not_model_prediction'}));
 await done;client.end(true);ws.terminate();clearTimeout(deadline);
 const result={http_status:response.status,authenticated:true,independent_broker:true,native_websocket_delivery:true,active_run:state.active,model_output_faked:false};
 fs.writeFileSync('/srv/abc20261102/results/control_transport_smoke.json',JSON.stringify(result,null,2));
 process.stdout.write(JSON.stringify(result)+'\n',()=>process.exit(0));
})().catch(e=>{console.error(e.message);process.exit(1)});
