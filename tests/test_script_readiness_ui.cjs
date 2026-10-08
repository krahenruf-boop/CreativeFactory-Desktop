// Execute the real workspace event handler with a DOM/API boundary fixture.
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const env={console,Map,Set,crypto:require('crypto').webcrypto,taskId:'job',setTimeout,clearTimeout};
vm.createContext(env);vm.runInContext(fs.readFileSync('web/workspace.js','utf8'),env);
vm.runInContext(`
var sent=[];
workspaceJob={id:'job',batch_review_view:{phase:'SCRIPTS',action_token:'ready',items:[{rank:'01',in_scope:true,selected_title:null,title_candidates:[]}]}};
workspacePage='SCRIPTS';
batchDrafts.set('job:01',{selected:'',customOpen:false,text:'',saved:''});
api=async(path,data)=>{sent.push(data);return {id:'job',batch_review_view:{phase:'TITLES',items:[{rank:'01',in_scope:true,selected_title:1,title_candidates:[{id:1,text:'Olha esta fivela',text_zh:'看看这个带扣'}]}]}}};
paintBatchFooter=()=>{};paintWorkspace=()=>{};loadJobs=async()=>{};toast=()=>{};$=()=>null;
`,env);
(async()=>{
 await vm.runInContext('submitBatch()',env);
 assert.equal(vm.runInContext('workspacePage',env),'TITLES');
 assert.equal(vm.runInContext("batchDraft(workspaceJob,'01').selected",env),'1');
 assert.equal(vm.runInContext("'selections' in sent[0]",env),false);
 assert.equal(vm.runInContext('sent.length',env),1);
 // Paste defaults derive from the current job, and the submitted original is
 // unchanged across the real UI handler -> request payload boundary.
 const review=fs.readFileSync('web/review.js','utf8');
 vm.runInContext(review,env);
 vm.runInContext(`var html='',payload;
 field=(label,input)=>label+input;options=(rows,selected)=>rows.map(([id,name])=>'<option value="'+id+'" '+(id===selected?'selected':'')+'>'+name+'</option>').join('');
 dialog=(title,body)=>{html=body;return {querySelector:selector=>({value:selector==='#pasted-text'?'Mira esta hebilla.\\nUn detalle para tu ropa.':'es-MX'})}};
 on=(root,selector,fn)=>{payload=fn};requestCopyDraft=(j,x,data)=>data;
 pasteModal({id:'job',target_language:'es-MX',market:'Mexico'},{rank:'01'},{});
 `,env);
 assert.match(vm.runInContext('html',env),/value="es-MX" selected/);
 assert.match(vm.runInContext('html',env),/使用这条脚本/);
 assert.equal(vm.runInContext('payload().text',env),'Mira esta hebilla.\nUn detalle para tu ropa.');
 assert.equal(vm.runInContext('payload().language',env),'es-MX');
 console.log('PASS: title default reset; script batch excludes title selection; paste target default and original payload (3 UI regressions)');
})().catch(e=>{console.error(e);process.exitCode=1});
