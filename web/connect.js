'use strict';
// The browser receives an HttpOnly cookie; no API token enters JavaScript.
const reconnectMessage=document.querySelector('#message');
const reconnectButton=document.querySelector('#retry');
reconnectButton.onclick=()=>location.reload();
(async()=>{
 const abort=new AbortController(),timer=setTimeout(()=>abort.abort(),8000);
 try{
  const response=await fetch('/session/connect',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:'{}',signal:abort.signal});
  if(!response.ok)throw Error('CONNECT_FAILED');
  // Detect disabled cookies instead of repeatedly navigating to this screen.
  const check=await fetch('/session/check',{credentials:'same-origin',signal:abort.signal});
  if(!check.ok){reconnectMessage.textContent='浏览器未保存连接凭证，请允许此网站使用 Cookie 后重新连接。';reconnectButton.hidden=false;return}
  location.replace('/');
 }catch{
  reconnectMessage.textContent='本机连接未能恢复，请稍后重新连接。';reconnectButton.hidden=false;
 }finally{clearTimeout(timer)}
})();
