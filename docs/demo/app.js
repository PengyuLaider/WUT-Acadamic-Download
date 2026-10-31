'use strict';
(() => {
  const byId = id => document.getElementById(id);
  const demo = document.querySelector('meta[name="demo-mode"]').content === 'true';
  let state = {status:'credentials_missing',has_username:false,has_password:false,auto_submit:false};
  let saving = false;
  byId('demo-banner').hidden = !demo;
  function feedback(message, error=false) {
    const node=byId('feedback');node.textContent=message;node.hidden=false;node.classList.toggle('error',error);
    node.setAttribute('role',error?'alert':'status');
  }
  function applyState(next, initial=false) {
    state=next;
    const ready=state.status==='ready';
    byId('config-tag').textContent=ready?'配置已就绪':state.status==='config_error'?'配置读取异常':state.status==='disabled'?'配置已停用':'尚未配置';
    byId('config-tag').classList.toggle('ready',ready);
    byId('step-config').classList.toggle('done',ready);byId('step-config').classList.toggle('active',!ready);
    byId('step-config').querySelector('.step-number').textContent=ready?'✓':'1';
    byId('step-config-state').textContent=ready?'已保存':'当前步骤';
    byId('step-auth').classList.toggle('active',ready);byId('step-auth-state').textContent=ready?'下一步':'待进行';
    byId('username').placeholder=state.has_username?'已保存账号，留空保留原账号':'请输入校园卡号 / 学工号';
    byId('password').placeholder=state.has_password?'已保存密码，留空保留原密码':'请输入智慧理工大密码';
    byId('user-required').textContent=state.has_username?'已保存':'必填';
    byId('password-required').textContent=state.has_password?'已保存':'必填';
    byId('username-help').textContent=state.has_username?'页面不回显原账号；修改时填写新账号。':'请输入你用于智慧理工大统一认证的账号。';
    byId('password-help').textContent=state.has_password?'无需重复输入；只有填写新密码才会替换。':'已保存的密码不会在页面中显示。';
    if(initial) byId('auto-submit').checked=Boolean(state.auto_submit);
    byId('save-label').textContent=ready?'保存配置修改':'保存账号配置';
    if(state.status==='config_error') feedback('原配置文件无法读取。请先检查文件格式，保存前不会覆盖原文件。',true);
  }
  function clearErrors() {
    for(const field of ['username','password']) {byId(field).removeAttribute('aria-invalid');byId(field+'-error').hidden=true;}
  }
  function fieldError(field,message) {
    byId(field).setAttribute('aria-invalid','true');byId(field+'-error').textContent=message;byId(field+'-error').hidden=false;
  }
  for(const field of ['username','password']) byId(field).addEventListener('input',()=>{byId(field).removeAttribute('aria-invalid');byId(field+'-error').hidden=true;});
  byId('password-toggle').addEventListener('click',()=>{
    const show=byId('password').type==='password';byId('password').type=show?'text':'password';
    byId('password-toggle').setAttribute('aria-label',show?'隐藏密码':'显示密码');byId('password-toggle').setAttribute('aria-pressed',String(show));byId('eye-slash').hidden=!show;
  });
  byId('instructions-button').addEventListener('click',()=>{
    const expanded=byId('instructions-button').getAttribute('aria-expanded')==='true';
    byId('instructions-button').setAttribute('aria-expanded',String(!expanded));byId('login-instructions').hidden=expanded;
  });
  byId('account-form').addEventListener('submit',async event=>{
    event.preventDefault();if(saving)return;clearErrors();
    const username=byId('username').value.trim(),password=byId('password').value;
    let first=null;
    if(!username&&!state.has_username){fieldError('username','请填写校园账号。');first='username';}
    if(!password&&!state.has_password){fieldError('password','请填写登录密码。');first=first||'password';}
    if(first){feedback('请补全标记的内容后再保存。',true);byId(first).focus();return;}
    if(state.status==='config_error'){feedback('请先修复原配置文件的格式，再刷新页面重试。',true);return;}
    saving=true;byId('save-button').disabled=true;byId('save-label').textContent='正在保存…';
    try {
      let next;
      if(demo){next={status:'ready',has_username:true,has_password:true,auto_submit:byId('auto-submit').checked,demo:true};}
      else{
        const response=await fetch('/save',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':document.querySelector('meta[name="csrf-token"]').content},body:JSON.stringify({username,password,auto_submit:byId('auto-submit').checked}),signal:AbortSignal.timeout(20000)});
        next=await response.json();if(!response.ok)throw new Error(next.error||'保存失败，请稍后重试。');
      }
      byId('username').value='';byId('password').value='';byId('password').type='password';
      byId('password-toggle').setAttribute('aria-label','显示密码');byId('password-toggle').setAttribute('aria-pressed','false');byId('eye-slash').hidden=true;
      applyState(next);feedback(demo?'演示保存完成，未写入任何账号密码。正式使用时，请运行本机配置界面。':'账号配置已保存在本机。下一步请运行登录入口，完成学校认证。');
      byId('feedback').focus();
    }catch(error){feedback(error.name==='TimeoutError'?'保存超时，请先确认本机服务仍在运行。':error.message||'连接失败，请确认本机服务仍在运行。',true);}
    finally{saving=false;byId('save-button').disabled=false;byId('save-label').textContent=state.status==='ready'?'保存配置修改':'保存账号配置';}
  });
  if(demo)applyState(state,true);
  else fetch('/status',{cache:'no-store',signal:AbortSignal.timeout(8000)}).then(response=>{if(!response.ok)throw new Error();return response.json();}).then(data=>applyState(data,true)).catch(()=>{byId('config-tag').textContent='服务连接失败';feedback('无法读取本机服务状态，请确认服务已启动后刷新页面。',true);});
})();
