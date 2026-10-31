'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('fs'),path=require('path');
const root=path.resolve(__dirname,'../..'),dictionary=require(path.join(root,'ui/i18n.js'));
test('UI provides translations for static text and labels',()=>{
 const html=fs.readFileSync(path.join(root,'ui/index.html'),'utf8');
 const texts=[...html.matchAll(/>([^<>]+)</g)].map(m=>m[1].trim()).filter(t=>/[\u4e00-\u9fff]/.test(t)&&!t.startsWith('.\\')&&!t.endsWith('.json')&&!['中文','Language / 语言'].includes(t));
 for(const text of texts)assert.ok(dictionary[text],`Missing translation: ${text}`);
});
test('dynamic validation and save states are translated',()=>{
 for(const key of ['配置已就绪','尚未配置','显示密码','隐藏密码','正在保存…','请填写校园账号。','请填写登录密码。','下一步'])assert.ok(dictionary[key]);
});
test('demo loads the same bilingual assets',()=>{
 const demo=fs.readFileSync(path.join(root,'docs/demo/index.html'),'utf8');assert.match(demo,/i18n.js/);
 assert.equal(fs.readFileSync(path.join(root,'ui/i18n.js'),'utf8'),fs.readFileSync(path.join(root,'docs/demo/i18n.js'),'utf8'));
});
