const test=require("node:test"),assert=require("node:assert/strict");
const fs=require("node:fs"),path=require("node:path"),os=require("node:os");
const {CoreClient}=require("../dist/test/core");
const root=path.resolve(__dirname,".."),python=path.resolve(root,"../../.venv/Scripts/python.exe");
const secret="PRIVATE_FLOW_BODY";
const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
async function until(read,details=()=>'',deadline=Date.now()+20000){
  let last;
  while(Date.now()<deadline){
    try{const value=await read();if(value)return value;}catch(error){last=error;if(!['ENOENT','EACCES','EBUSY'].includes(error.code)&&!String(error).includes('OPERATION_FAILED'))throw error;}
    await sleep(50);
  }
  throw new Error('Flow fixture timed out: '+details()+' '+String(last||''));
}
async function fixture(t){
  const project=fs.mkdtempSync(path.join(os.tmpdir(),"patchport-flow-body-"));
  const config=path.join(project,"bridge.json"),database=path.join(project,".agent-bridge/queue.sqlite3"),script=path.join(project,"mock_codex.py");
  fs.writeFileSync(path.join(project,"input.txt"),secret);
  fs.writeFileSync(script,"import json,sys\nfrom pathlib import Path\nsys.stdin.read()\nPath(sys.argv[sys.argv.index('--output-last-message')+1]).write_text('PRIVATE_FLOW_BODY')\nprint(json.dumps({'type':'turn.completed','usage':{'input_tokens':2,'output_tokens':3}}))\n");
  const endpoint={adapter:"codex",command:[python,script],sandbox:"read-only"};
  fs.writeFileSync(config,JSON.stringify({project:".",include:["input.txt"],writable:["input.txt"],endpoints:{mock:endpoint}}));
  fs.writeFileSync(path.join(project,"role_workflow.json"),JSON.stringify({developer:endpoint,reviewer:endpoint,checks:[[python,"-c","pass"]]}));
  const core=new CoreClient(python,path.join(root,"core"));
  t.after(async()=>{core.dispose();await sleep(250);assert.ok(path.resolve(project).startsWith(path.resolve(os.tmpdir())+path.sep));fs.rmSync(project,{recursive:true,force:true});});
  assert.equal((await core.request("capabilities")).records_flow_schema,1);
  for(const action of ["records.prepare.preview","records.roles.prepare.preview"]){
    const plan=await core.request(action,{project,config});assert.ok(plan.plan_id);
    await core.request("records.apply",{plan_id:plan.plan_id,apply:true});
  }
  const submission=await core.request("roles.preview",{config,flow_id:"flow-a",prompt:secret});
  await core.request("operation.apply",{plan_id:submission.plan_id,apply:true});
  const receipt=path.join(project,'.agent-bridge/p1-jobs/flow-a.json'),log=path.join(project,'.agent-bridge/p1-jobs/flow-a.log');
  const details=()=>[receipt,log].map(file=>{try{return fs.readFileSync(file,'utf8').slice(-4000);}catch(error){return String(error);}}).join('\n');
  const deadline=Date.now()+20000;
  await until(async()=>{
    const ticket=JSON.parse(fs.readFileSync(receipt,'utf8'));
    if(ticket.state==='FAILED')throw new Error('Flow fixture failed: '+details());
    if(ticket.state!=='FINISHED')return false;
    assert.equal(ticket.workflow_state,'REVIEW_DONE',details());return true;
  },details,deadline);
  const plan=await until(async()=>{const p=await core.request("records.flow.purge.preview",{project,config,flow_id:"flow-a"});return p.plan_id&&p;},details,deadline);
  assert.equal(plan.report.task_ids.length,2);assert.equal(plan.report.flow,"flow-a");
  const before=(await core.request("project.inspect",{project,config})).control.control;
  assert.equal(before.calls_started,2);
  return {project,config,database,core,plan,before};
}

test("flow body API removes both actual role jobs and refuses same flow ID",{timeout:30000},async t=>{
  const f=await fixture(t);
  const rows=await f.core.request("records.inventory",{project:f.project,config:f.config,kind:"task"});
  assert.equal(rows.items.every(item=>!item.body_selectable),true);
  const result=await f.core.request("records.apply",{plan_id:f.plan.plan_id,apply:true});assert.equal(result.body_deleted,true);
  const listing=await f.core.request("records.inventory",{project:f.project,config:f.config,kind:"flow"});
  assert.equal(listing.items[0].body_state,"DELETED");assert.equal(listing.items[0].body_selectable,false);
  await assert.rejects(f.core.request("roles.preview",{config:f.config,flow_id:"flow-a",prompt:secret}),/FLOW_ID_ALREADY_RECORDED/);
  await assert.rejects(f.core.request("roles.inspect",{config:f.config,flow_id:"flow-a",role:"reviewer"}),/BODY_DELETED/);
  assert.deepEqual((await f.core.request("project.inspect",{project:f.project,config:f.config})).control.control,f.before);
  assert.equal(fs.readFileSync(path.join(f.project,"input.txt"),"utf8"),secret);
  assert.deepEqual(fs.readdirSync(path.join(f.project,".role-flows/flow-a")),["result.json"]);
  assert.equal(fs.existsSync(path.join(f.project,".agent-bridge/p1-jobs/flow-a.txt")),false);
});

test("flow partial receipt survives pipe replacement and resumes only file cleanup",{timeout:30000},async t=>{
  const f=await fixture(t),file=path.join(f.project,".role-flows/flow-a/original-input/input.txt");
  fs.chmodSync(file,0o444);
  let partial;await assert.rejects(f.core.request("records.apply",{plan_id:f.plan.plan_id,apply:true}),error=>{partial=error.result;return partial?.state==="PARTIAL";});
  f.core.dispose();const reader=new CoreClient(python,path.join(root,"core"));t.after(()=>reader.dispose());
  const listed=await reader.request("records.inventory",{project:f.project,config:f.config,kind:"flow"});
  assert.equal(listed.pending_body_operations[0].operation,partial.operation);assert.equal(listed.items[0].body_selectable,false);
  const receipt=await reader.request("records.operation",{project:f.project,database:f.database,operation:partial.operation});assert.equal(receipt.complete,false);
  const quarantine=path.join(f.project,".agent-bridge/record-bodies",partial.operation,"flow");fs.chmodSync(path.join(quarantine,"original-input/input.txt"),0o666);
  const resume=await reader.request("records.resume.preview",{project:f.project,config:f.config,operation:partial.operation});
  const done=await reader.request("records.apply",{plan_id:resume.plan_id,apply:true});assert.equal(done.body_deleted,true);
  assert.equal(fs.existsSync(quarantine),false);assert.equal(JSON.stringify(done).includes(secret),false);
  assert.deepEqual((await reader.request("project.inspect",{project:f.project,config:f.config})).control.control,f.before);
});
