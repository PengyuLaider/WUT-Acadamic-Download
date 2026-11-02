'use strict';
const test = require('node:test'), assert = require('node:assert/strict');
const fs = require('fs'), os = require('os'), path = require('path');
const {parseArgs, isPdfCandidateUrl, classifySource, isLoginWallPage, EXIT} = require('../scripts/browser_dl'); // Import must not create a browser or directories.
const {Cdp, publicUrl, sanitize, basicPdf, requiresUser} = require('../scripts/browser_support');
const args = (...s) => parseArgs(['node','script',...s]);

test('search lists candidates by default', () => {
  const a=args('--site','cnki','--search','battery'); assert.equal(a.select,null); assert.match(a.url,/cnki/);
});
test('explicit selection', () => assert.equal(args('--search','battery','--select','2').select,2));
test('bad or missing arguments', () => {
  for (const input of [['--url'],['--url','https://example.org','--out'],['--url','https://e.org','--timeout','10x'],['--url','https://e.org','--wait-user','0'],['--search','x','--select','0'],['--typo'],['javascript:alert(1)']])
    assert.throws(()=>args(...input));
});
test('help requires no browser',()=>assert.equal(args('--help').help,true));
test('DOI normalization',()=>assert.equal(args('10.1234/test').url,'https://doi.org/10.1234/test'));
test('URL credentials rejected',()=>assert.throws(()=>args('--url','https://user:pass@example.org')));
test('signed URLs redacted and document IDs preserved',()=>{
  const u=publicUrl('https://user:pass@example.org/abstract?v=article&token=secret&Signature=x#auth');
  assert.equal(u,'https://example.org/abstract?v=article');
  assert.equal(sanitize({urls:['https://e.org/pdf?token=secret']}).urls[0],'https://e.org/pdf');
});
test('challenge recognition does not mistake institution link for login',()=>{
  assert.equal(requiresUser({url:'https://e.org/article',bodyText:'Access through your institution'}),false);
  for(const p of [{captchaEl:true},{passwordEl:true},{url:'https://e.org/login'},{bodyText:'Verify you are human'}]) assert.equal(requiresUser(p),true);
});
test('file detector rejects HTML, oversized files and truncated PDFs',()=>{
  const dir=fs.mkdtempSync(path.join(os.tmpdir(),'campus-test-')); const f=path.join(dir,'paper.pdf');
  try {
    fs.writeFileSync(f,'%PDF-1.4\ncontent');assert.equal(basicPdf(f),false);
    fs.writeFileSync(f,'%PDF-1.4\ncontent\n%%EOF');assert.equal(basicPdf(f),true);assert.equal(basicPdf(f,8),false);
    fs.writeFileSync(f,'<html>login</html>');assert.equal(basicPdf(f),false);
  } finally {fs.rmSync(dir,{recursive:true});}
});
class FakeSocket extends EventTarget {
  send(msg){this.last=JSON.parse(msg);}
  close(){this.dispatchEvent(new Event('close'));}
  result(data){this.dispatchEvent(new MessageEvent('message',{data:JSON.stringify(data)}));}
}
test('CDP response resolves and clears pending',async()=>{
  const ws=new FakeSocket(),cdp=new Cdp(ws,100);const p=cdp.send('Page.enable');
  ws.result({id:ws.last.id,result:{ok:true}});assert.deepEqual(await p,{ok:true});assert.equal(cdp.pending.size,0);cdp.close();
});
test('CDP timeout rejects',async()=>{
  const ws=new FakeSocket(),cdp=new Cdp(ws,20);await assert.rejects(cdp.send('Never.answers'),/timeout/);assert.equal(cdp.pending.size,0);cdp.close();
});
test('CDP disconnect rejects all requests',async()=>{
  const ws=new FakeSocket(),cdp=new Cdp(ws,500);const p=cdp.send('Page.enable');ws.close();await assert.rejects(p,/disconnected/);assert.equal(cdp.pending.size,0);
});

// P0-1：只接受明确 PDF 下载地址，参考文献/配图/静态资源不能被当全文
test('PDF candidate whitelist rejects references/images',()=>{
  for (const u of [
    'https://www.sciencedirect.com/science/article/pii/S0016236124020027/pdfft?md5=x&pid=1-s2.0-S0016236124020027-main.pdf',
    'https://ieeexplore.ieee.org/stampPDF/getPDF.jsp?tp=&arnumber=771073',
    'https://onlinelibrary.wiley.com/doi/pdfdirect/10.1002/x',
    'https://example.org/paper.pdf',
    'https://bar.cnki.net/bar/download/order?id=x',
    'https://oss.wanfangdata.com.cn/file/download/perio_x.aspx',
  ]) assert.equal(isPdfCandidateUrl(u),true,u);
  for (const u of [
    'https://ars.els-cdn.com/content/image/1-s2.0-S0016236124020027-gr1_lrg.jpg',
    'https://example.org/logo.svg',
    'https://example.org/references',
  ]) assert.equal(isPdfCandidateUrl(u),false,u);
  // 参考文献里的其他文章 pdfft：给定当前文章 PII 时必须排除
  assert.equal(isPdfCandidateUrl('https://www.sciencedirect.com/science/article/pii/S0016236123020239/pdfft?md5=y','S0016236124020027'),false);
});

// P0-2：WebVPN/CAS 登录墙不能被当作文章页
test('login wall detection',()=>{
  assert.equal(isLoginWallPage({title:'资源访问控制系统',url:'https://webvpn.whut.edu.cn/login',bodyText:'账号登录'}),true);
  assert.equal(isLoginWallPage({title:'统一身份认证',url:'https://zhlgd.whut.edu.cn/tpass/login',bodyText:'登录'}),true);
  assert.equal(isLoginWallPage({title:'论文标题 - ScienceDirect',url:'https://www.sciencedirect.com/science/article/pii/X',bodyText:'Full text access'}),false);
  assert.equal(isLoginWallPage({title:'论文标题',url:'https://example.org/article',bodyText:'Access through your institution'}),false);
  assert.equal(isLoginWallPage({title:'登录',url:'https://example.org/login',passwordEl:true,bodyText:'请登录'}),true);
});

// P2-8：来源/版本标注（禁止把预印本当出版稿）
test('source and version classification',()=>{
  assert.deepEqual(classifySource('https://arxiv.org/pdf/2106.09685','10.48550/arXiv.2106.09685'),{sourceType:'repository',versionType:'preprint'});
  assert.deepEqual(classifySource('https://www.sciencedirect.com/science/article/pii/X','10.1016/j.fuel.2024.132853'),{sourceType:'publisher',versionType:'published'});
  assert.deepEqual(classifySource('https://example.org/x','10.1234/x'),{sourceType:'unknown',versionType:'unknown'});
});

test('exit code contract',()=>{
  assert.deepEqual(EXIT,{OK:0,ERROR:1,NEED_USER:2,PARTIAL:3});
  assert.throws(()=>args('--url','https://e.org','--version-type','draft'));
  assert.throws(()=>args('--url','https://e.org','--source-type','preprint'));
  assert.equal(args('--url','https://e.org','--version-type','preprint').versionType,'preprint');
  assert.equal(args('--url','https://e.org','--skip-identity-check').skipIdentityCheck,true);
});

test('verification requires title and DOI agreement and full-text review',()=>{
 const {verificationStatus}=require('../scripts/browser_dl');
 assert.equal(verificationStatus({available:true,valid:true,doi_match:true,identity:'unverified'}),'saved_pending_verification');
 assert.equal(verificationStatus({available:true,valid:true,identity:'matched',review_required:['single_page']}),'saved_pending_verification');
 assert.equal(verificationStatus({available:true,valid:true,text_available:true,title_checkable:true,title_match:false}),'wrong_document');
 assert.equal(verificationStatus({available:false}),'saved_pending_verification');
 assert.equal(verificationStatus({available:true,valid:true,identity:'matched'}),'downloaded');
});
test('click candidates cannot select high resolution images or another PII',()=>{
 const {isPdfCandidateUrl}=require('../scripts/browser_dl');
 for(const url of ['https://ars.els-cdn.com/a.pdf','https://example.org/a.pdf.jpg','https://example.org/pii/S999/pdfft'])assert.equal(isPdfCandidateUrl(url,'S123'),false);
 assert.equal(isPdfCandidateUrl('https://pubs.acs.org/iecred/article-pdf/44/16/a.pdf',''),true);
});
