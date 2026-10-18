const test=process.platform==='win32'?require('node:test'):require('node:test').skip,assert=require("node:assert/strict");
const fs=require("node:fs"),path=require("node:path"),os=require("node:os");
const {CoreClient}=require("../dist/test/core");
const root=path.resolve(__dirname,".."),python=require('./helpers.cjs').python;
const secret="PRIVATE_BATCH_BODY";
const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
async function until(read){for(let i=0;i<160;i++){try{const value=await read();if(value)return value;}catch{}await sleep(50);}throw new Error("Fixture timed out");}
async function fixture(t){
  const project=fs.mkdtempSync(path.join(require('./helpers.cjs').temporaryRoot,"patchport-body-"));
  const config=path.join(project,"bridge.json"),database=path.join(project,".agent-bridge/queue.sqlite3");
  fs.writeFileSync(path.join(project,"input.txt"),secret);
  fs.writeFileSync(config,JSON.stringify({project:".",include:["input.txt"],writable:["input.txt"],endpoints:{mock:{adapter:"command",command:[python,"-c",`print('${secret}')`]}}}));
  const core=new CoreClient(python,path.join(root,"core"));let runId;
  t.after(async()=>{if(runId)await core.request("runner.stop",{config,run_id:runId,apply:true,cancel_active:true}).catch(()=>{});core.dispose();await sleep(150);assert.ok(path.resolve(project).startsWith(path.resolve(require('./helpers.cjs').temporaryRoot)+path.sep));fs.rmSync(project,{recursive:true,force:true});});
  assert.equal((await core.request("capabilities")).records_body_schema,1);
  const prepare=await core.request("records.prepare.preview",{project,config});assert.ok(prepare.plan_id);
  await core.request("records.apply",{plan_id:prepare.plan_id,apply:true});
  const inventory=await core.request("context.inventory",{config});
  const submission=await core.request("batch.preview",{config,batch_id:"fixture",targets:["mock"],prompt:secret,plan:{schema:1,files:inventory.files.map(file=>({path:file.path,mode:"full",reason:"Fixture explicit input"}))}});
  await core.request("operation.apply",{plan_id:submission.plan_id,apply:true});
  const started=await core.request("runner.start",{config,apply:true});runId=started.run_id;
  await until(async()=>{const result=await core.request("result",{config,task_id:"fixture--mock"});return result.state==="DONE"&&result;});
  const active=await core.request("records.purge.preview",{project,config,task_ids:["fixture--mock"]});assert.equal(active.plan_id,null);
  await core.request("runner.stop",{config,run_id:runId,apply:true,cancel_active:false});runId=undefined;
  const plan=await until(async()=>{const value=await core.request("records.purge.preview",{project,config,task_ids:["fixture--mock"]});return value.plan_id&&value;});
  const before=(await core.request("project.inspect",{project,config})).control.control;
  for(const log of fs.readdirSync(path.join(project,".agent-bridge/runner-logs")))assert.equal(fs.readFileSync(path.join(project,".agent-bridge/runner-logs",log),"utf8").includes(secret),false);
  return {project,config,database,core,plan,before,task:"fixture--mock"};
}

test("protected batch survives actual managed runner then resolves a lost purge response on another pipe",{timeout:30000},async t=>{
  const f=await fixture(t),exchange=f.core.exchange.bind(f.core);
  f.core.exchange=async(action,args)=>{const result=await exchange(action,args);if(action==="records.apply")throw new Error("Injected response loss");return result;};
  await assert.rejects(f.core.request("records.apply",{plan_id:f.plan.plan_id,apply:true}),/response loss/);
  f.core.dispose();const reader=new CoreClient(python,path.join(root,"core"));t.after(()=>reader.dispose());
  const receipt=await reader.request("records.operation",{project:f.project,database:f.database,operation:f.plan.plan_id});
  assert.equal(receipt.complete,true);assert.equal(receipt.body_deleted,true);
  const result=await reader.request("result",{config:f.config,task_id:f.task});assert.equal(result.answer.status,"BODY_DELETED");assert.equal(JSON.stringify(result).includes(secret),false);
  const listed=await reader.request("records.inventory",{project:f.project});assert.equal(listed.items[0].body_state,"DELETED");assert.equal(listed.items[0].body_selectable,false);
  assert.deepEqual((await reader.request("project.inspect",{project:f.project,config:f.config})).control.control,f.before);
  assert.equal(fs.existsSync(path.join(f.project,".agent-bridge/workspaces",f.task)),false);
  assert.equal(fs.readFileSync(path.join(f.project,"input.txt"),"utf8"),secret);
});

test("filesystem failure leaves a discoverable receipt and explicit resume performs only remaining cleanup",{timeout:30000},async t=>{
  const f=await fixture(t),home=path.join(f.project,".agent-bridge/workspaces",f.task);
  fs.chmodSync(path.join(home,"project/input.txt"),0o444);
  let partial;
  await assert.rejects(f.core.request("records.apply",{plan_id:f.plan.plan_id,apply:true}),error=>{partial=error.result;assert.equal(partial.state,"PARTIAL");return true;});
  const listed=await f.core.request("records.inventory",{project:f.project});assert.equal(listed.pending_body_operations[0].operation,partial.operation);
  const status=await f.core.request("records.operation",{project:f.project,database:f.database,operation:partial.operation});assert.equal(status.complete,false);
  await assert.rejects(f.core.request("runner.start",{config:f.config,apply:true}),/BODY_PURGE_PENDING/);
  const quarantine=path.join(f.project,".agent-bridge/record-bodies",partial.operation,f.task);
  assert.equal(fs.existsSync(home),false);fs.chmodSync(path.join(quarantine,"project/input.txt"),0o666);
  const resume=await f.core.request("records.resume.preview",{project:f.project,config:f.config,operation:partial.operation});
  const done=await f.core.request("records.apply",{plan_id:resume.plan_id,apply:true});assert.equal(done.state,"DONE");assert.equal(done.operation,partial.operation);
  assert.deepEqual((await f.core.request("project.inspect",{project:f.project,config:f.config})).control.control,f.before);
  assert.equal(fs.existsSync(quarantine),false);
});
