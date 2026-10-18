const test=require("node:test"),assert=require("node:assert/strict");
const fs=require("node:fs"),path=require("node:path"),os=require("node:os"),crypto=require("node:crypto");
const {CoreClient}=require("../dist/test/core");
const root=path.resolve(__dirname,".."),python=path.resolve(root,"../../.venv/Scripts/python.exe");
const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
async function fixture(t){
  const project=fs.mkdtempSync(path.join(os.tmpdir(),"patchport-remove-")),config=path.join(project,"bridge.json");
  const raw=JSON.stringify({project:".",include:["input.txt"],writable:["input.txt"],endpoints:{mock:{adapter:"command",command:[python,"-c","pass"]}}});
  fs.writeFileSync(config,raw);fs.writeFileSync(path.join(project,"input.txt"),"ORIGINAL");fs.writeFileSync(path.join(project,"bridge.py"),"pass\n");
  fs.writeFileSync(path.join(project,".bridge-integration.json"),JSON.stringify({schema:1,python,files:{"bridge.py":crypto.createHash("sha256").update("pass\n").digest("hex")}}));
  const core=new CoreClient(python,path.join(root,"core"));t.after(async()=>{core.dispose();await sleep(100);assert.ok(path.resolve(project).startsWith(path.resolve(os.tmpdir())+path.sep));fs.rmSync(project,{recursive:true,force:true});});
  assert.equal((await core.request("capabilities")).connection_remove_schema,1);
  const preparation=await core.request("records.prepare.preview",{project,config});await core.request("records.apply",{plan_id:preparation.plan_id,apply:true});
  const plan=await core.request("connection.remove.preview",{project,config,paths:["bridge.py","bridge.json"]});assert.ok(plan.plan_id);
  return {project,config,raw,core,plan,database:path.join(project,".agent-bridge/queue.sqlite3")};
}
test("connection removal survives pipe replacement and requires explicit reconnect",{timeout:20000},async t=>{
  const f=await fixture(t);const result=await f.core.request("connection.apply",{plan_id:f.plan.plan_id,apply:true});assert.equal(result.complete,true);
  f.core.dispose();const reader=new CoreClient(python,path.join(root,"core"));t.after(()=>reader.dispose());
  const scope={project:f.project,database:f.database,operation:result.operation};
  assert.equal((await reader.request("connection.operation",scope)).complete,true);
  assert.equal((await reader.request("project.inspect",{project:f.project})).linked,false);
  fs.writeFileSync(f.config,f.raw);
  assert.equal((await reader.request("project.inspect",{project:f.project})).linked,false);
  const reconnect=await reader.request("connection.reconnect.preview",scope);
  assert.equal((await reader.request("connection.apply",{plan_id:reconnect.plan_id,apply:true})).reconnected,true);
  const inspected=await reader.request("project.inspect",{project:f.project});assert.equal(inspected.linked,true);assert.equal(inspected.control.control.calls_started,0);
  assert.equal(fs.existsSync(path.join(f.project,"bridge.py")),false);assert.equal(fs.readFileSync(path.join(f.project,"input.txt"),"utf8"),"ORIGINAL");
});
test("manifest failure after config removal remains resumable with no configuration file",{timeout:20000},async t=>{
  const f=await fixture(t),manifest=path.join(f.project,".bridge-integration.json");fs.chmodSync(manifest,0o444);
  let partial;await assert.rejects(f.core.request("connection.apply",{plan_id:f.plan.plan_id,apply:true}),error=>{partial=error.result;return partial?.state==="PARTIAL";});
  assert.equal(fs.existsSync(f.config),false);assert.equal(partial.deleted,2);
  const inventory=await f.core.request("connection.inventory",{project:f.project});assert.equal(inventory.receipt.operation,partial.operation);
  fs.chmodSync(manifest,0o666);
  const scope={project:f.project,database:f.database,operation:partial.operation};
  const resume=await f.core.request("connection.resume.preview",scope);assert.ok(resume.plan_id);
  assert.equal((await f.core.request("connection.apply",{plan_id:resume.plan_id,apply:true})).complete,true);
  assert.equal(fs.existsSync(f.config),false);assert.equal(fs.readFileSync(path.join(f.project,"input.txt"),"utf8"),"ORIGINAL");
});
