// Review preparation stays actionable in both list and detail, including polling.
const fs=require('fs'),vm=require('vm'),assert=require('assert'),crypto=require('crypto');
const nodes={
 '#tasks':{innerHTML:'',querySelectorAll:()=>[]},'#task-count':{},'#user-action-count':{},
 '.batch-rows':{innerHTML:''},'.batch-footer':{innerHTML:'',querySelectorAll:()=>[]},'.workspace-scroll':{scrollTop:0},
};
const root={innerHTML:'',querySelector:s=>nodes[s]||null,querySelectorAll:()=>[]};
nodes['#task-workspace']=root;
const document={querySelector:s=>nodes[s]||null,querySelectorAll:()=>[]};
const env={console,crypto,document,window:{},setTimeout,clearTimeout};
vm.createContext(env);
vm.runInContext(fs.readFileSync('web/app.js','utf8').split("on(document,'#settings',settingsModal);")[0],env);
vm.runInContext(fs.readFileSync('web/workspace.js','utf8'),env);
env.fixture={id:'review',job_name:'待补齐检查',product_name:'钱包',mode:'Transport',market:'Brazil',target_language:'pt-BR',state:'REVIEW_REQUIRED',stage:'文案',ui_status:'口播待补齐',source_count:10,created:1000,elapsed:0,progress:0,items:[],ownership_counts:{completed:0},task_control:{actions:[]},batch_review_view:{phase:'SCRIPTS',action_token:'fixture',items:[],expected:10,script_ready_count:9,all_scripts_ready:false}};
vm.runInContext('jobs=[fixture];jobFilter="mine";paintJobs();workspaceJob=fixture;taskId=fixture.id;workspacePage="SCRIPTS";paintWorkspace()',env);
assert.match(nodes['#tasks'].innerHTML,/badge orange[^>]*>口播待补齐/);
assert.equal(nodes['#task-count'].textContent,'1 个任务');
assert.equal(nodes['#user-action-count'].hidden,false);
assert.match(root.innerHTML,/badge orange[^>]*>口播待补齐/);
assert.match(nodes['.batch-footer'].innerHTML,/9 \/ 10 口播已准备/);
assert.match(nodes['.batch-footer'].innerHTML,/<button[^>]*id="batch-submit"[^>]*disabled/);
// Only the label changes: item records and the confirmation token stay equal.
vm.runInContext('jobs=[{...fixture,ui_status:"待确认口播"}];refreshWorkspace()',env);
assert.match(root.innerHTML,/badge orange[^>]*>待确认口播/);
vm.runInContext('fixture={...fixture,ui_status:"标题待补齐",batch_review_view:{...fixture.batch_review_view,phase:"TITLES",title_ready_count:9,all_titles_ready:false}};jobs=[fixture];workspaceJob=fixture;workspacePage="TITLES";paintJobs();paintWorkspace()',env);
assert.match(root.innerHTML,/badge orange[^>]*>标题待补齐/);
assert.match(nodes['.batch-footer'].innerHTML,/<button[^>]*id="batch-submit"[^>]*disabled/);
assert.equal(vm.runInContext('jobStatusColor({ui_status:"失败"})',env),'red');
console.log('PASS: pending script/title list and detail, pending filter, disabled barriers, label-only refresh, genuine failure color');
