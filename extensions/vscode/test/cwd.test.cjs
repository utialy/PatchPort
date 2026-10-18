const test=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs/promises'),path=require('node:path'),os=require('node:os');
const {Router,defaultBoard}=require('../dist/test/model'),{SessionHost}=require('../dist/test/session');
test('PTY events accept the reported canonical cwd and reject another directory',async t=>{
 const root=await fs.mkdtemp(path.join(require('./helpers.cjs').temporaryRoot,'patchport-cwd-')),actual=path.join(root,'actual'),alias=path.join(root,'alias'),outside=path.join(root,'outside');
 await fs.mkdir(actual);await fs.mkdir(outside);await fs.symlink(actual,alias,process.platform==='win32'?'junction':'dir');
 const board=defaultBoard(),node={id:'a',name:'a',provider:'claude',profileId:'a',project:alias,groupId:'team',x:0,y:0};board.nodes=[node];
 const router=new Router(board,()=>{}),host=new SessionHost(router,path.join(root,'storage'),path.resolve(__dirname,'..'));
 t.after(async()=>{host.dispose();assert.ok(path.resolve(root).startsWith(path.resolve(require('./helpers.cjs').temporaryRoot)+path.sep));await fs.rm(root,{recursive:true,force:true});});
 const session={node,cwd:await fs.realpath(actual),profile:{provider:'claude'},turn:0,disposed:false};
 router.started('a');await host.event(session,{cwd:session.cwd,session_id:'session',hook_event_name:'SessionStart'});
 assert.equal(router.runtime.get('a').phase,'ready');assert.equal(session.eventSession,'session');
 await host.event(session,{cwd:session.cwd.toUpperCase(),session_id:'session',hook_event_name:'SessionStart'});
 assert.equal(router.runtime.get('a').phase,process.platform==='win32'?'ready':'attention');
 await host.event(session,{cwd:outside,session_id:'session',hook_event_name:'UserPromptSubmit',prompt:'wrong location'});
 assert.equal(router.runtime.get('a').phase,'attention');
});
