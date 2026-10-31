'use strict';
const test = require('node:test'), assert = require('node:assert/strict');
const fs = require('fs'), os = require('os'), path = require('path');
const {parseArgs} = require('../scripts/browser_dl'); // Import must not create a browser or directories.
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
