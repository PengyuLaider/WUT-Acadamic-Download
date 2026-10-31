'use strict';
const test = require('node:test'), assert = require('node:assert/strict');
const fs = require('fs'), os = require('os'), path = require('path'), vm = require('vm');
const {spawnSync} = require('child_process');
const {approvedOrigin,readCredentials,credentialStatus,fillLogin} = require('../scripts/login_credentials');
const {parseArgs} = require('../scripts/browser_dl');
const FAKE = {enabled:true,username:'offline-fake-account',password:'fake-ONLY-秘密-\\" = +',login_url:'https://webvpn.whut.edu.cn/login?cas_login=true',auto_submit:false};

function configFile(body) {
  const dir=fs.mkdtempSync(path.join(os.tmpdir(),'login-config-test-'));
  const file=path.join(dir,'test.private.json');fs.writeFileSync(file,body);
  return {file, clean:()=>fs.rmSync(dir,{recursive:true,force:true})};
}
function browser({origin='https://zhlgd.whut.edu.cn',action='https://zhlgd.whut.edu.cn/tpass/login',captcha=false,navigate=false,missing=false,throwSecret=false}={}) {
  const state={focus:null,clicks:0,inputs:[],expressions:[]};
  const location={origin,href:origin+'/tpass/login'};
  const button={innerText:'登录',disabled:false,getBoundingClientRect:()=>({width:80,height:30}),click:()=>state.clicks++};
  const form={action,querySelectorAll:()=>[button]};
  const field=()=>({value:'',disabled:false,readOnly:false,form,getBoundingClientRect:()=>({width:100,height:30}),focus(){state.focus=this;},select(){this.value='';}});
  state.user=field();state.password=field();
  const document={querySelectorAll(selector){
    if(selector==='input[type="password"]') return missing?[]:[state.password];
    if(selector.includes('input[name="un"]')) return missing?[]:[state.user];
    if(selector.includes('input[name="code"]')) return captcha?[{getBoundingClientRect:()=>({width:100,height:30})}]:[];
    return [button];
  }};
  const cdp={async send(method,params){
    if(method==='Runtime.evaluate'){
      state.expressions.push(params.expression);
      return {result:{value:vm.runInNewContext(params.expression,{location,document,URL})}};
    }
    if(method==='Input.insertText'){
      if(throwSecret)throw new Error('do not leak '+FAKE.password);
      state.focus.value=params.text;state.inputs.push(params.text);
      if(navigate){location.origin='https://evil.example';location.href='https://evil.example/login';}
      return {};
    }
    throw new Error('unexpected method');
  }};
  return {cdp,state};
}

test('private file supports Unicode, BOM and password special characters',()=>{
  const tmp=configFile('\uFEFF'+JSON.stringify(FAKE));try{assert.equal(readCredentials(tmp.file).password,FAKE.password);}finally{tmp.clean();}
});
test('readiness returns booleans/status only',()=>{
  const tmp=configFile(JSON.stringify(FAKE));try{
    const result=credentialStatus(tmp.file);assert.equal(result.status,'ready');
    const text=JSON.stringify(result);assert.ok(!text.includes(FAKE.username));assert.ok(!text.includes(FAKE.password));
  }finally{tmp.clean();}
});
test('empty fields return missing names',()=>{
  const tmp=configFile(JSON.stringify({...FAKE,username:'',password:''}));try{assert.deepEqual(credentialStatus(tmp.file).missing_fields,['username','password']);}finally{tmp.clean();}
});
test('malformed JSON error never includes secret source text',()=>{
  const tmp=configFile('FAKE_SECRET_MALFORMED '+FAKE.password);try{assert.equal(credentialStatus(tmp.file).reason,'credential_file_unreadable_or_invalid_json');}finally{tmp.clean();}
});
test('credentials confined to explicit school HTTPS origins',()=>{
  for(const url of ['http://zhlgd.whut.edu.cn','https://zhlgd.whut.edu.cn.evil.example','https://user:pass@zhlgd.whut.edu.cn','https://unknown.whut.edu.cn','javascript:alert(1)'])assert.equal(approvedOrigin(url),false);
  assert.equal(approvedOrigin(FAKE.login_url),true);
});
test('fills visible fields by native input without exposing values in page scripts',async()=>{
  const b=browser();const result=await fillLogin(b.cdp,'test-session',FAKE);
  assert.equal(result.status,'credentials_filled');assert.equal(b.state.user.value,FAKE.username);assert.equal(b.state.password.value,FAKE.password);assert.equal(b.state.clicks,0);
  assert.ok(b.state.expressions.every(x=>!x.includes(FAKE.password)&&!x.includes(FAKE.username)));
});
test('auto submit clicks only once without CAPTCHA',async()=>{
  const b=browser();const result=await fillLogin(b.cdp,'test-session',{...FAKE,auto_submit:true});assert.equal(result.submitted,true);assert.equal(b.state.clicks,1);
});
test('auto submit pauses for CAPTCHA',async()=>{
  const b=browser({captcha:true});const result=await fillLogin(b.cdp,'test-session',{...FAKE,auto_submit:true});assert.equal(result.status,'need_user');assert.equal(b.state.clicks,0);
});
test('untrusted page receives no credentials',async()=>{
  const b=browser({origin:'https://evil.example'});await fillLogin(b.cdp,'session',FAKE);assert.equal(b.state.inputs.length,0);
});
test('cross-site form action receives no credentials',async()=>{
  const b=browser({action:'https://evil.example/post'});await fillLogin(b.cdp,'session',FAKE);assert.equal(b.state.inputs.length,0);
});
test('navigation between fields prevents password entry',async()=>{
  const b=browser({navigate:true});const result=await fillLogin(b.cdp,'session',FAKE);assert.equal(result.status,'origin_not_allowed');assert.deepEqual(b.state.inputs,[FAKE.username]);
});
test('CDP exceptions are replaced with fixed errors',async()=>{
  const b=browser({throwSecret:true});const result=await fillLogin(b.cdp,'session',FAKE);assert.equal(result.status,'login_fill_failed');assert.ok(!JSON.stringify(result).includes(FAKE.password));
});
test('missing fields and disabled config do not submit',async()=>{
  const b=browser({missing:true});assert.equal((await fillLogin(b.cdp,'session',FAKE)).status,'login_fields_not_found');
  assert.equal((await fillLogin(b.cdp,'session',{...FAKE,enabled:false})).status,'disabled');assert.equal(b.state.clicks,0);assert.equal(b.state.inputs.length,0);
});
test('CLI check never prints account/password and starts no browser',()=>{
  const tmp=configFile(JSON.stringify(FAKE));try{
    const result=spawnSync(process.execPath,[path.resolve(__dirname,'../scripts/login_credentials.js'),'--check','--config',tmp.file],{encoding:'utf8'});
    assert.equal(result.status,0);assert.equal(JSON.parse(result.stdout).status,'ready');assert.ok(!result.stdout.includes(FAKE.username));assert.ok(!result.stdout.includes(FAKE.password));assert.equal(result.stderr,'');
  }finally{tmp.clean();}
});
test('login arguments are separate from paper downloads',()=>{
  const parse=(...a)=>parseArgs(['node','script',...a]);assert.equal(parse('--login').login,true);assert.equal(parse('--login','--credentials','private.json').credentials,'private.json');
  assert.throws(()=>parse('--login','--url','https://example.org'));assert.throws(()=>parse('--login','--credentials'));
});
