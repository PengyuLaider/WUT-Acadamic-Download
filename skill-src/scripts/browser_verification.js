'use strict';
const fs=require('fs'),path=require('path');
const {spawnSync}=require('child_process');

function verifySaved(saved,doi,title,run=spawnSync){
  const python=process.env.CAMPUS_PYTHON||process.env.DSH_PYTHON||'python';
  const result=run(python,[path.join(__dirname,'pdf_check.py'),saved.path,'--doi',doi||'','--title',title||''],
    {encoding:'utf8',timeout:60000,windowsHide:true,maxBuffer:1024*1024});
  let verification;
  try{verification=JSON.parse(result.stdout);}catch{
    verification={valid:null,identity:'unverified',reason:'verification_process_unavailable',parser:'unavailable'};
  }
  const rejected=verification.valid===false || (verification.text_available===true && verification.title_checkable===true && title && verification.title_match===false);
  if(rejected){
    const dir=path.join(path.dirname(saved.path),'quarantine');fs.mkdirSync(dir,{recursive:true});
    let target=path.join(dir,path.basename(saved.path)),n=2;
    while(fs.existsSync(target))target=path.join(dir,n+++'_'+path.basename(saved.path));
    fs.renameSync(saved.path,target);
    return {status:verification.valid===false?'invalid_pdf':'identity_mismatch',path:target,verification};
  }
  const complete=verification.valid===true&&verification.identity==='matched'&&!(verification.review_required||[]).length;
  return {status:complete?'downloaded':'saved_pending_verification',path:saved.path,verification};
}
module.exports={verifySaved};
