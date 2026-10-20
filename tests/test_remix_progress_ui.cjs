// Exercise list/detail rendering and polling while Remix has no output rows yet.
const fs=require('fs'),vm=require('vm'),assert=require('assert'),crypto=require('crypto');
const nodes={
 '#tasks':{innerHTML:'',querySelectorAll:()=>[]},
 '#task-count':{},'#job-filter option[value=deleted]':{},
 '.batch-rows':{innerHTML:''},'.batch-footer':{innerHTML:'',querySelectorAll:()=>[]},
 '.workspace-scroll':{scrollTop:0},
};
const root={innerHTML:'',querySelector:s=>nodes[s]||null,querySelectorAll:()=>[]};
nodes['#task-workspace']=root;
const clockNode={dataset:{jobCreated:'1000'}};
const document={querySelector:s=>nodes[s]||null,querySelectorAll:s=>s==='[data-job-created]'?[clockNode]:[]};
let now=3100;
const env={console,crypto,document,window:{},setTimeout,clearTimeout,Date:class extends Date{static now(){return now*1000}}};
vm.createContext(env);
vm.runInContext(fs.readFileSync('web/app.js','utf8').split("on(document,'#settings',settingsModal);")[0],env);
vm.runInContext(fs.readFileSync('web/workspace.js','utf8'),env);
env.fixture={id:'remix',job_name:'混剪检查',product_name:'摆件',mode:'Remix',market:'Brazil',target_language:'pt-BR',state:'RUNNING',stage:'分析',ui_status:'分析中',source_count:30,output_count:15,analyzed_count:12,created:1000,elapsed:0,progress:0,items:[],ownership_counts:{completed:0,processing:0},task_control:{worker_active:true,actions:['PAUSE','STOP']},batch_review_view:{phase:'SCRIPTS',action_token:'unchanged',items:[],expected:15,script_ready_count:0,all_scripts_ready:false}};
vm.runInContext('jobs=[fixture];jobFilter="running";paintJobs();workspaceJob=fixture;taskId=fixture.id;workspacePage="SCRIPTS";paintWorkspace()',env);
assert.match(nodes['#tasks'].innerHTML,/已分析 12 \/ 30/);
assert.match(nodes['#tasks'].innerHTML,/width:40%/);
assert.match(nodes['#tasks'].innerHTML,/创建至今 35:00/);
assert.match(root.innerHTML,/30 条素材/);
assert.match(root.innerHTML,/计划 15 条成片/);
assert.match(nodes['.batch-rows'].innerHTML,/已分析 12 \/ 30/);
assert.doesNotMatch(nodes['.batch-footer'].innerHTML,/确认全部口播/);
// New source checkpoint must update the open detail even if items/token/state stay equal.
vm.runInContext('jobs=[{...fixture,analyzed_count:13}];refreshWorkspace()',env);
assert.match(nodes['.batch-rows'].innerHTML,/已分析 13 \/ 30/);
// Unchanged API data must not freeze the creation-age clock.
now+=4;vm.runInContext('refreshJobClocks()',env);
assert.equal(clockNode.textContent,'创建至今 35:04');
assert.equal(vm.runInContext('sourceAnalysisProgress({...fixture,analyzed_count:30}).label',env),'编排混剪方案');
assert.equal(vm.runInContext('sourceAnalysisProgress({...fixture,state:"PAUSED",ui_status:"已暂停"}).working',env),false);
assert.equal(vm.runInContext('sourceAnalysisProgress({...fixture,mode:"Transport"})',env),null);
assert.equal(vm.runInContext('sourceAnalysisProgress({...fixture,stage:"文案",items:[{rank:"01"}]})',env),null);
assert.equal(vm.runInContext('sourceAnalysisProgress({...fixture,state:"COMPLETED"})',env),null);
assert.equal(vm.runInContext('jobTiming({...fixture,state:"COMPLETED",elapsed:125})',env),'<span>已用 2:05</span>');
console.log('PASS: source progress, list filter, live detail checkpoint refresh, creation-age clock, selection/paused/completed states');
