/* Personal-Mac task bridge setup; passwords are never included in status. */
async function taskBridgeSettings(container){
 const path='/integrations/feishu-tasks';
 let state=await api(path);
 const paint=()=>{
  if(!container.isConnected)return;
  container.innerHTML=`<div class="settings-row"><strong>飞书接单</strong><span class="state"><i class="dot ${state.connected?'ok':''}"></i>${state.connected?'个人 Mac 已连接':state.state==='BLOCKED'?'夸克不可用，接单已暂停':state.enabled?'连接中，请检查应用配置':'已关闭'}</span><button id="bridge-refresh">刷新状态</button></div>
   <p class="muted">同事在专用群提交夸克素材链接，由本机执行。接单期间请保持联网、唤醒并登录桌面。</p>
   <div class="settings-row"><label for="bridge-enabled">连接机器人</label><input id="bridge-enabled" type="checkbox" ${state.enabled?'checked':''}></div>
   <div class="settings-row"><label for="bridge-accepting">正式接单</label><input id="bridge-accepting" type="checkbox" ${state.accepting?'checked':''} ${state.verified?'':'disabled'}></div>
   <small>${state.verified?'完整链路已验收，可开启正式接单。':'验收前仅管理员可以提交三条视频测试任务。'}</small>
   <div class="settings-row"><span>应用凭据</span><span>${state.configured?'已保存到独立钥匙串':'尚未配置'}</span><button id="bridge-credentials" ${state.enabled?'disabled':''}>配置应用</button></div>
   <div class="settings-row"><span>专用群</span><span>${state.group_bound?'已绑定':'等待绑定'}</span><button id="bridge-bind" ${state.group_bound?'disabled':''}>生成绑定码</button></div>
   <div id="bridge-code" class="section-gap" role="status"></div>
   <div class="settings-row"><span>夸克连接</span><span>${state.quark_health?.ready?'当前服务授权检查通过':state.quark_installed?'已安装，尚未通过当前服务检查':'尚未安装'}</span></div>
   ${state.error?`<div class="warning">${esc(state.error_message||state.error)}</div>`:''}
   ${state.quark_health?.notice?`<div class="warning">${esc(state.quark_health.notice)}</div>`:''}
   ${state.tasks.length?`<details class="section-gap"><summary>最近接单 ${state.tasks.length} 个</summary>${state.tasks.map(t=>`<p><strong>${esc(t.product_name)}</strong> · ${esc(t.id)}<br><small>${esc(t.state)}${t.error?' · '+esc(t.error):''}</small></p>`).join('')}</details>`:''}`;
  const refresh=async()=>{state=await api(path);paint()};
  on(container,'#bridge-refresh',refresh);
  for(const [id,key]of [['bridge-enabled','enabled'],['bridge-accepting','accepting']])bind(container,'#'+id,'change',async e=>{const button=e.target;button.disabled=true;try{state=await api(path,{[key]:button.checked});paint()}catch(error){button.checked=!button.checked;throw error}finally{if(button.isConnected)button.disabled=false}});
  on(container,'#bridge-credentials',()=>{
   const d=dialog('飞书接单机器人',`<small>使用独立企业自建应用，启用机器人、群内 @消息接收及卡片操作回调。凭据只保存到当前 Mac。</small><div class="field section-gap"><label for="bridge-app-id">App ID</label><input id="bridge-app-id" autocomplete="off" value="${esc(state.app_id||'')}" placeholder="cli_…"></div><div class="field"><label for="bridge-app-secret">App Secret</label><input id="bridge-app-secret" type="password" autocomplete="off"></div><div class="modal-actions"><button data-close>取消</button><button class="primary" id="bridge-save">保存</button></div>`,{cls:'small'});
   on(d,'#bridge-save',async()=>{const b=d.querySelector('#bridge-save');b.disabled=true;try{await api(path,{app_id:d.querySelector('#bridge-app-id').value.trim(),app_secret:d.querySelector('#bridge-app-secret').value.trim()});d.querySelector('#bridge-app-secret').value='';d.close();await refresh()}finally{if(b.isConnected)b.disabled=false}});
  });
  on(container,'#bridge-bind',async()=>{const result=await api(path+'/binding-code',{});container.querySelector('#bridge-code').innerHTML=`<p>在新建专用群里，由你发送：</p><p><strong>@Creative Factory 自动化剪辑 绑定 ${esc(result.code)}</strong></p><small>十分钟内有效。首次绑定的人会成为管理员，请勿把绑定码交给其他人。</small>`});
 };
 paint();
}
