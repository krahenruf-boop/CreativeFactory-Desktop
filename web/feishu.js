/* Feishu is a local cached reference source. Remote reads are explicit only. */
let feishuStatus=null,feishuSheet=null,feishuRequest=0;
function copySourceMarkup(){
 return `<select id="copy-source" aria-label="口播文案来源">${options([['auto','自动生成'],['feishu','飞书文案库'],['local','本地上传表格']],draft.copySource||'auto')}</select>${draft.copySource==='feishu'?`<div class="reference-line"><span id="feishu-match" class="ellipsis muted">${esc(feishuSheet?.sheet_name||'按产品和市场匹配本地缓存')}</span><button class="link" id="choose-feishu">选择文案</button><button class="quiet" id="refresh-feishu" aria-label="刷新文案库" title="重新同步飞书文案库">↻</button></div>`:''}`;
}
function clearFeishuSelection(){
 feishuRequest++;feishuSheet=null;
 if(referenceData?.source_type==='feishu'){referenceData=null;referencePath=null;clearScriptSelection();if(draft.scriptMode==='TABLE_SELECTED')draft.scriptMode='AUTO'}
}
function bindCopySource(root){
 bind(root,'#copy-source','change',e=>{clearFeishuSelection();draft.copySource=e.target.value;referenceData=null;referencePath=null;clearScriptSelection();draft.scriptMode='AUTO';renderForm();if(draft.copySource==='feishu')return matchFeishu()});
 on(root,'#choose-feishu',async()=>{if(!feishuSheet)await matchFeishu(true);if(!feishuSheet)return;draft.scriptMode='TABLE_SELECTED';renderForm();return scriptPicker()});
 on(root,'#refresh-feishu',async()=>{const b=root.querySelector('#refresh-feishu');b.disabled=true;try{feishuStatus=await api('/copy-sources/feishu/sync',{});clearFeishuSelection();await matchFeishu(true);toast('文案库已同步')}finally{if(b.isConnected)b.disabled=false}});
}
async function matchFeishu(explicit=false){
 if(draft.copySource!=='feishu')return;
 const request=++feishuRequest,product=draft.productName.trim(),marketName=draft.market;
 if(!product){if(explicit)toast('请先填写产品名称');return}
 const matches=await api('/copy-sources/feishu/matches?'+new URLSearchParams({product,market:marketName}));
 if(request!==feishuRequest||draft.copySource!=='feishu')return;
 if(!matches.length){if($('#feishu-match'))$('#feishu-match').textContent='没有匹配的产品工作表';if(explicit)toast('没有匹配的产品工作表，请检查产品名称和市场');return}
 const exact=matches.filter(s=>s.exact);
 let chosen=exact.length===1&&matches.length===1?exact[0]:null;
 if(!chosen){
  if(!explicit){if($('#feishu-match'))$('#feishu-match').textContent='有近似工作表，请选择确认';return}
  chosen=await new Promise(resolve=>{const d=dialog('选择产品工作表',`<div class="picker-list">${matches.map(s=>`<button class="feishu-sheet-choice" data-sheet="${esc(s.sheet_id)}">${esc(s.sheet_name)} <small>${s.copy_count} 条</small></button>`).join('')}</div>`,{cls:'small',sub:'不会自动使用近似产品文案'});d.addEventListener('close',()=>resolve(null),{once:true});d.querySelectorAll('[data-sheet]').forEach(b=>on(d,b,()=>{resolve(matches.find(s=>s.sheet_id===b.dataset.sheet));d.close()}))});
 }
 if(!chosen||request!==feishuRequest)return;
 feishuStatus=await api('/copy-sources/feishu');
 const ref=await api('/copy-sources/feishu/reference?'+new URLSearchParams({source_id:feishuStatus.binding.source_id,sheet_id:chosen.sheet_id,market:marketName}));
 if(request!==feishuRequest||product!==draft.productName.trim()||marketName!==draft.market)return;
 feishuSheet=chosen;referencePath=null;referenceData=ref;clearScriptSelection();renderForm();
}
async function feishuSettings(c){
 feishuStatus=await api('/copy-sources/feishu');if(!c.isConnected)return;
 const paint=()=>{const s=feishuStatus,b=s.binding||{};
 c.innerHTML=`<div class="field"><label for="feishu-name">文案库名称</label><input id="feishu-name" maxlength="80" value="${esc(b.name||'我的飞书文案库')}"></div><div class="field"><label for="feishu-url">飞书链接</label><input id="feishu-url" type="url" value="${esc(b.url||'')}" placeholder="https://…feishu.cn/wiki/…?sheet=…"></div><div class="between"><small>${s.credentials_configured?'应用凭据已配置 · 只读':'需要配置飞书应用只读授权'}</small><button id="feishu-auth">配置应用</button></div><div class="feishu-status" role="status">${b.connection_status==='CONNECTED'?'已连接':b.connection_status==='ERROR'?'连接未完成':'尚未测试连接'}${s.last_synced_at?` · ${s.sheet_count} 个工作表 · ${s.copy_count} 条文案<br>最近同步：${new Date(s.last_synced_at*1000).toLocaleString('zh-CN')}`:''}</div><div id="feishu-error" class="warning"></div>${s.sheets?.some(x=>x.warnings.length)?`<small>${s.sheets.filter(x=>x.warnings.includes('NON_COPY_SHEET')).length} 个非口播工作表已跳过；${s.sheets.filter(x=>x.warnings.includes('FEISHU_HEADERS_UNKNOWN')).length} 个表头待识别。来源语言冲突会在文案选择时提示。</small>`:''}<small>文案保存在本机。仅点击同步或刷新时读取飞书，不修改原表。删除绑定不会修改已有任务。</small><div class="modal-actions"><button id="feishu-unbind" class="quiet danger" ${b.source_id?'':'disabled'}>删除绑定</button><button id="feishu-test">测试连接</button><button class="primary" id="feishu-sync">立即同步</button></div>`;
 const save=()=>api('/copy-sources/feishu/bind',{name:c.querySelector('#feishu-name').value.trim(),url:c.querySelector('#feishu-url').value.trim()});
 const act=async(kind)=>{const buttons=[...c.querySelectorAll('button')];buttons.forEach(b=>b.disabled=true);c.querySelector('.feishu-status').textContent=kind==='sync'?'正在读取并同步全部工作表…':'正在验证文档和指定工作表…';try{await save();feishuStatus=await api('/copy-sources/feishu/'+kind,{});clearFeishuSelection();paint();toast(kind==='sync'?'文案库同步完成':'连接成功，可立即同步')}catch(e){feishuStatus=await api('/copy-sources/feishu');paint();c.querySelector('#feishu-error').textContent=e.message}};
 on(c,'#feishu-test',()=>act('test'));on(c,'#feishu-sync',()=>act('sync'));on(c,'#feishu-auth',()=>feishuCredentials(async()=>{feishuStatus=await api('/copy-sources/feishu');paint()}));
 on(c,'#feishu-unbind',async()=>{feishuStatus=await api('/copy-sources/feishu/unbind',{});clearFeishuSelection();renderForm();paint();toast('绑定已移除，飞书原表保持不变')});
 };paint();
}
function feishuCredentials(done){
 const d=dialog('飞书只读应用',`<small>在飞书开放平台创建自建应用，开通读取知识库节点、读取电子表格元数据和内容的权限。发布后，将此文档授予应用可阅读权限。</small><div class="field section-gap"><label for="feishu-app-id">App ID</label><input id="feishu-app-id" autocomplete="off" placeholder="cli_…"></div><div class="field"><label for="feishu-app-secret">App Secret</label><input id="feishu-app-secret" type="password" autocomplete="off" placeholder="只保存到 macOS 钥匙串，不回显"></div><div class="modal-actions"><button data-close>取消</button><button class="primary" id="save-feishu-credentials">保存到钥匙串</button></div>`,{cls:'small'});
 on(d,'#save-feishu-credentials',async()=>{const b=d.querySelector('#save-feishu-credentials');b.disabled=true;try{await api('/copy-sources/feishu/credentials',{app_id:d.querySelector('#feishu-app-id').value.trim(),app_secret:d.querySelector('#feishu-app-secret').value.trim()});d.querySelector('#feishu-app-secret').value='';d.close();await done()}finally{if(b.isConnected)b.disabled=false}});
}
