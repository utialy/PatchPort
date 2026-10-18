const test = require("node:test"), assert = require("node:assert/strict");
const fs = require("node:fs"), path = require("node:path"), os = require("node:os"), cp = require("node:child_process");
const { CoreClient } = require("../dist/test/core");
const root = path.resolve(__dirname, ".."), python = path.resolve(root, "../../.venv/Scripts/python.exe");
const secret = "SECRET_BODY_DO_NOT_RETURN";
const prepare = String.raw`
import sys
from pathlib import Path
from agent_bridge.storage import Store, atomic_json, load_config
from agent_bridge import workspace
root=Path(sys.argv[1]).resolve()
(root/'input.txt').write_text('SECRET_BODY_DO_NOT_RETURN')
atomic_json(root/'bridge.json',dict(project='.',include=['input.txt'],writable=['input.txt'],endpoints={'mock':dict(adapter='command',command=['never-run','SECRET_BODY_DO_NOT_RETURN'])}))
config=load_config(root/'bridge.json'); store=Store(config['state'])
for index in range(27):
    task=store.submit('job-'+str(index).zfill(2),['mock'],'SECRET_BODY_DO_NOT_RETURN',config['endpoints'])[0]
    home=workspace.create(config,task)
    (home/'answer.txt').write_text('SECRET_BODY_DO_NOT_RETURN')
    store.claim(task); store.finish(task,'DONE',dict(workspace=str(home),answer={'text':'SECRET_BODY_DO_NOT_RETURN'}))
`;
function fixture(t) {
  const project=fs.mkdtempSync(path.join(os.tmpdir(),"patchport-records-"));
  t.after(()=>{ assert.ok(path.resolve(project).startsWith(path.resolve(os.tmpdir())+path.sep)); fs.rmSync(project,{recursive:true,force:true}); });
  const prepared=cp.spawnSync(python,["-B","-X","utf8","-c",prepare,project],{windowsHide:true,encoding:"utf8",env:{...process.env,PYTHONPATH:path.join(root,"core")}});
  assert.equal(prepared.status,0,prepared.stderr);
  const core=new CoreClient(python,path.join(root,"core")); t.after(()=>core.dispose());
  return {project,core,db:path.join(project,".agent-bridge/queue.sqlite3")};
}
function snapshot(root) {
  const result={};
  for(const entry of fs.readdirSync(root,{withFileTypes:true})) {
    const file=path.join(root,entry.name);
    if(entry.isDirectory()) for(const [name,value] of Object.entries(snapshot(file))) result[entry.name+"/"+name]=value;
    else result[entry.name]=require("node:crypto").createHash("sha256").update(fs.readFileSync(file)).digest("hex");
  }
  return result;
}
test("records API pages actual tasks and complete references without exposing bodies or mutating files", async t=>{
  const {project,core}=fixture(t), before=snapshot(project);
  assert.equal((await core.request("capabilities")).records_schema,1);
  const first=await core.request("records.inventory",{project,limit:25});
  assert.equal(first.items.length,25); assert.equal(first.more,true);
  const next=await core.request("records.inventory",{project,limit:25,cursor:first.next_cursor});
  assert.deepEqual(next.items.map(item=>item.id),["job-25--mock","job-26--mock"]);
  let cursor="", ids=[], summary;
  do {
    const detail=await core.request("records.inspect",{project,kind:"task",record_id:"job-00--mock",limit:5,cursor});
    assert.equal(JSON.stringify(detail).includes(secret),false); assert.equal(detail.complete,true);
    assert.equal(detail.mutation_supported,false); assert.equal(detail.plan_id,undefined);
    if(summary) assert.deepEqual(detail.summary,summary); else summary=detail.summary;
    ids.push(...detail.references.map(ref=>ref.id)); cursor=detail.next_cursor||"";
  } while(cursor);
  assert.equal(new Set(ids).size,summary.references); assert.deepEqual(snapshot(project),before);
});
test("unsafe DB observations and overlapping config return explicit unknown with no repair",async t=>{
  const {project,core,db}=fixture(t);
  fs.writeFileSync(db+"-wal","pending fixture"); const before=snapshot(project);
  const page=await core.request("records.inventory",{project});
  assert.equal(page.complete,false); assert.ok(page.blockers.includes("DATABASE_UNREADABLE")); assert.deepEqual(snapshot(project),before);
  fs.unlinkSync(db+"-wal");
  const other=path.join(project,"other.json"); const config=JSON.parse(fs.readFileSync(path.join(project,"bridge.json"),"utf8")); config.state=".agent-bridge/nested"; fs.writeFileSync(other,JSON.stringify(config));
  const report=await core.request("records.inspect",{project,kind:"task",record_id:"job-00--mock",known_configs:[other]});
  assert.ok(report.blockers.includes("SHARED_STATE_UNCONFIRMED"));
  await assert.rejects(core.request("records.inventory",{project,apply:true}),/INVALID_ARGUMENTS/);
  await assert.rejects(core.request("records.apply",{project,apply:true}),/INVALID_ARGUMENTS/);
});
