"""Create a public source bundle from an explicit allowlist; never read private files."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import zipfile

ROOT=Path(__file__).resolve().parent
FILES=['README.md','SECURITY.md','.gitignore','requirements-dev.txt','campus_portal.py','登录配置界面.py','填写登录配置.ps1','登录图书馆.ps1','检查本地环境.ps1','登录配置.example.json','export_public.py']
TREES=['ui','docs','skill-src','.github']

def allowed(path):
    parts=path.parts
    return not any(p in ('__pycache__','papers','browser-profile','.git','qa') or p.startswith('备份_') for p in parts) and not any(x in path.name for x in ('.private.','.pyc','.tmp','cred.xml')) and path.as_posix()!='skill-src/config/config.json'

def export(destination):
    destination=Path(destination).resolve()
    if destination.exists(): raise ValueError('输出目录已存在，请选择新的空目录，避免覆盖。')
    sources=[ROOT/f for f in FILES]
    for name in TREES:
        sources.extend(p for p in (ROOT/name).rglob('*') if p.is_file() and allowed(p.relative_to(ROOT)))
    destination.mkdir(parents=True)
    manifest=[]
    for source in sources:
        if source.is_symlink(): raise ValueError('发布源不允许符号链接')
        relative=source.relative_to(ROOT)
        if not allowed(relative): raise ValueError('发布清单包含禁止文件')
        target=destination/relative;target.parent.mkdir(parents=True,exist_ok=True)
        if source.suffix=='.md':
            text=source.read_text(encoding='utf-8').replace('C:\\Users\\40635','%USERPROFILE%')
            target.write_text(text,encoding='utf-8')
        else: shutil.copyfile(source,target)
        manifest.append({'path':relative.as_posix(),'sha256':hashlib.sha256(target.read_bytes()).hexdigest()})
    (destination/'release-manifest.json').write_text(json.dumps({'files':manifest,'private_files_included':False,'papers_included':False},indent=2),encoding='utf-8')
    archive=destination.with_suffix('.zip')
    if archive.exists(): raise ValueError('ZIP 已存在，拒绝覆盖')
    with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as output:
        for p in destination.rglob('*'):
            if p.is_file(): output.write(p,p.relative_to(destination))
    return {'directory':str(destination),'zip':str(archive),'files':len(manifest),'private_files_included':False,'papers_included':False}

if __name__=='__main__':
    parser=argparse.ArgumentParser(description='导出不含账号、论文和浏览器数据的 GitHub 源码包')
    parser.add_argument('--out',type=Path,default=ROOT/'GitHub发布包'/'campus-library-papers')
    args=parser.parse_args()
    print(json.dumps(export(args.out),ensure_ascii=False,indent=2))
