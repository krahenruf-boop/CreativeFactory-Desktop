// Real request handler with a DOM/API boundary fixture; no browser dependencies.
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const env={console,Map,Set};vm.createContext(env);
vm.runInContext(fs.readFileSync('web/review.js','utf8'),env);
vm.runInContext(`
var posts=[],reads=0,closed=false,updated=false,message='';
var button={disabled:false,textContent:'使用这条脚本',isConnected:true};
var form={querySelector:()=>button,close:()=>{closed=true}};
var job={id:'job'},item={rank:'02',action_token:'old'};
var data={mode:'PASTED',text:'原始输入必须保留'};
var draft={id:'saved',action_token:'new'},fresh={id:'job',items:[{rank:'02',action_token:'new',script_draft:draft}]};
api=async(path,body,options)=>{
 if(body){posts.push({body,options});if(posts.length===1)throw Object.assign(Error('provider error'),{code:'TITLE_LANGUAGE_MISMATCH'});return draft}
 reads++;return fresh;
};
returnToContent=async(j,x)=>{updated=x.script_draft.id==='saved'};
toast=text=>{message=text};
`,env);
(async()=>{
 await assert.rejects(vm.runInContext('requestCopyDraft(job,item,data,{},form)',env),/provider error/);
 assert.equal(env.closed,false);assert.equal(env.button.disabled,false);assert.equal(env.item.action_token,'new');assert.equal(env.data.text,'原始输入必须保留');assert.equal(env.posts.length,1);
 await vm.runInContext('requestCopyDraft(job,item,data,{},form)',env);
 assert.equal(env.posts[1].body.action_token,'new');assert.equal(env.posts[1].options.preserveDialog,true);assert.equal(env.closed,true);assert.equal(env.updated,true);assert.match(env.message,/口播已保存/);
 // A genuine concurrent change keeps the dialog/text, and does not auto retry.
 env.closed=false;env.api=async(path,body)=>{if(body)throw Object.assign(Error('stale'),{code:'STALE_ACTION'});return env.fresh};
 await assert.rejects(vm.runInContext('requestCopyDraft(job,item,data,{},form)',env),/文案已保留/);assert.equal(env.closed,false);
 // Never claim success unless the saved draft is visible in a fresh snapshot.
 env.api=async(path,body)=>body?{id:'different'}:env.fresh;
 await assert.rejects(vm.runInContext('requestCopyDraft(job,item,data,{},form)',env),/尚未保存/);assert.equal(env.closed,false);
 console.log('PASS: retry uses fresh token, preserves text/dialog, no automatic POST, verified persistence before success');
})().catch(e=>{console.error(e);process.exitCode=1});
