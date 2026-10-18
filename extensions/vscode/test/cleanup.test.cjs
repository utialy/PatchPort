const test=require("node:test"),assert=require("node:assert/strict");
const fs=require("node:fs"),path=require("node:path"),os=require("node:os"),crypto=require("node:crypto");
const {CoreClient}=require("../dist/test/core");
const root=path.resolve(__dirname,".."),python=path.resolve(root,"../../.venv/Scripts/python.exe");
async function fixture(t){
 const project=fs.mkdtempSync(path.join(os.tmpdir(),"patchport-cleanup-")),config=path.join(project,"bridge.json"),database=path.join(project,".agent-bridge/queue.sqlite3");
 fs.writeFileSync(config,JSON.stringify({project:".",include:["input.txt"],writable:["input.txt"],endpoints:{mock:{adapter:"command",command:[python,"-c","pass"]}}}));
 fs.writeFileSync(path.join(project,"input.txt"),"ORIGINAL");fs.writeFileSync(path.join(project,"bridge.py"),"pass\n");
 fs.writeFileSync(path.join(project,".bridge-integration.json"),JSON.stringify({schema:1,python,files:{"bridge.py":crypto.createHash("sha256").update("pass\n").digest("hex")}}));
 const core=new CoreClient(python,path.join(root,"core"));t.after(async()=>{core.dispose();await new Promise(r=>setTimeout(r,100));assert.ok(path.resolve(project).startsWith(path.resolve(os.tmpdir())+path.sep));fs.chmodSync(path.join(project,".bridge-integration.json"),0o666);fs.rmSync(project,{recursive:true,force:true});});
 assert.equal((await core.request("capabilities")).project_cleanup_schema,1);
 const preparation=await core.request("records.prepare.preview",{project,config});await core.request("records.apply",{plan_id:preparation.plan_id,apply:true});
 return {project,config,database,core};
}
test("cleanup stages require separate review and survive config-free pipe replacement",{timeout:20000},async t=>{
 const f=await fixture(t),args={database:f.database,project:f.project};
 await f.core.request("interactive.owner.open",{...args,owner:"owner",apply:true});
 await f.core.request("interactive.begin",{...args,owner:"owner",session_id:"session",node:"node",provider:"codex",name:"PRIVATE",apply:true});
 await f.core.request("interactive.record",{database:f.database,owner:"owner",session_id:"session",event_id:"body",kind:"completed",data:{answer:"PRIVATE"},apply:true});
 await f.core.request("interactive.record",{database:f.database,owner:"owner",session_id:"session",event_id:"stop",kind:"session_stopped",data:{confirmed_exit:true},apply:true});
 await f.core.request("interactive.end",{database:f.database,owner:"owner",apply:true});
 const plan=await f.core.request("cleanup.preview",{project:f.project,config:f.config,selection:{history:[{database:f.database,session_ids:["session"]}],paths:["bridge.py","bridge.json"]}});
 assert.deepEqual(plan.report.stages.map(s=>s.kind),["history","connection"]);
 const first=await f.core.request("cleanup.apply",{plan_id:plan.plan_id,apply:true});assert.equal(first.scope_complete,false);assert.equal(fs.existsSync(f.config),true);
 const scope={project:f.project,database:f.database,operation:first.operation};
 const next=await f.core.request("cleanup.next.preview",scope);assert.equal(next.report.next_kind,"connection");
 assert.equal((await f.core.request("cleanup.apply",{plan_id:next.plan_id,apply:true})).scope_complete,true);
 f.core.dispose();const reader=new CoreClient(python,path.join(root,"core"));t.after(()=>reader.dispose());
 assert.equal((await reader.request("cleanup.operation",scope)).scope_complete,true);
 assert.equal((await reader.request("cleanup.inventory",{project:f.project})).operations[0].operation,first.operation);
 assert.equal(fs.readFileSync(path.join(f.project,"input.txt"),"utf8"),"ORIGINAL");
});
test("cleanup partial metadata resumes the same child operation after config removal",{timeout:20000},async t=>{
 const f=await fixture(t),manifest=path.join(f.project,".bridge-integration.json");fs.chmodSync(manifest,0o444);
 const plan=await f.core.request("cleanup.preview",{project:f.project,config:f.config,selection:{paths:["bridge.py","bridge.json"]}});
 let result;await assert.rejects(f.core.request("cleanup.apply",{plan_id:plan.plan_id,apply:true}),e=>{result=e.result;return result?.state==="PARTIAL";});
 assert.equal(fs.existsSync(f.config),false);const child=result.stages[0].operation;
 const scope={project:f.project,database:f.database,operation:result.operation};
 assert.equal((await f.core.request("cleanup.operation",scope)).scope_complete,false);fs.chmodSync(manifest,0o666);
 const next=await f.core.request("cleanup.next.preview",scope),done=await f.core.request("cleanup.apply",{plan_id:next.plan_id,apply:true});
 assert.equal(done.scope_complete,true);assert.equal(done.stages[0].operation,child);assert.equal(done.residual.forensic_erasure,false);
});
