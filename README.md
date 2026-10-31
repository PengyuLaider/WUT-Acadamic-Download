# 校园文献助手 · Campus Library Papers

本地运行的校园网论文获取工具与智能体 skill。支持账号配置、学校认证辅助、OA 定位、指定论文下载和 PDF 核验。默认学校为武汉理工大学。

![校园门户风格配置界面](docs/images/portal-desktop.jpg)

> 独立工具，非学校官方认证页面。学校账号登录尚待现场验收；配置就绪不等于学校已认证或所有论文都有订阅权限。

## 功能

| 能力 | 说明 |
|---|---|
| 校园门户界面 | 白底深蓝、响应式布局、行内反馈、密码切换 |
| 接入流程可视化 | 区分账号保存、学校认证、全文下载核验 |
| 本机私密配置 | 页面不回显已保存账号密码，不接入外部服务 |
| 无账号演示 | 页面模拟保存，可用于 GitHub 展示 |
| 论文获取 | DOI、文章 URL、清单；OA 或学校授权路径 |
| 文件核验 | PDF 结构、题名/DOI、哈希与索引 |

## 快速开始

Python 3.10+。配置界面只用标准库，无需 npm、pip 或前端构建。

```bash
python campus_portal.py
```

浏览器打开本机页面，在界面填写校园账号和密码后保存。终端保持前台运行，按 Ctrl+C 停止。不要关闭安全软件、恢复被隔离的旧启动脚本或为本工具添加白名单。

仅展示界面、不要读取或保存真实账号时：

```bash
python campus_portal.py --demo
```

也可以直接打开 [静态演示](docs/demo/index.html)，或者把 `docs` 作为 GitHub Pages 的发布目录，访问其中的 `demo/`。静态演示不是远程账号登录服务。

## 继续学校认证与论文下载

浏览器 CLI 需要 Node 22+ 和 Edge/Chrome。只有宿主允许这种自动化时使用；否则通过宿主支持的浏览器工具操作。

```bash
node skill-src/scripts/browser_dl.js --login --credentials 登录配置.private.json
node skill-src/scripts/browser_dl.js --url "文章URL" --out papers --no-proxy
```

配置界面保存在完整工具包根目录。验证码、扫码和手机验证由本人完成；不要反复自动重试错误密码。独立浏览器与其他浏览器会话不自动共享登录。

`--no-proxy` 让独立浏览器按正常网络直连；仅在系统代理影响校园授权时按需选择，不保证所有网络都可用。

PDF 结构和篇目核验需要可选 `pypdf`：

```bash
python -m pip install -r requirements-dev.txt
python skill-src/scripts/paper_oa.py fetch "10.1016/j.esci.2023.100174" --out papers
python skill-src/scripts/paper_oa.py import-file "已下载.pdf" --doi "目标DOI" --title "完整题名" --out papers
python skill-src/scripts/paper_oa.py organize papers
```

浏览器保存后先标记 `saved_pending_verification`，核验成功后再记完成。预印本、出版稿及附件需分清。

## 智能体 skill

`skill-src/SKILL.md` 是可安装的 skill 入口，名称为 `campus-library-papers`，按宿主支持的安装方式使用。单独安装 skill 不包含根目录的配置界面；凭据文件需要显式指定路径。

## 开发与测试

```bash
python -m unittest discover -s skill-src/tests -p "test_*.py" -v
node --test skill-src/tests/test_browser.js skill-src/tests/test_login.js
python skill-src/scripts/webvpn_url.py --selftest
```

Python 测试仅使用假账号和临时目录；不会登录学校。Windows 网络诊断另见 `skill-src/tests/test_network.ps1`。GitHub Actions 配置在 `.github/workflows/test.yml`，CI 是否通过以仓库实际运行结果为准。

界面核验记录：[界面与验证](docs/界面与验证.md)。

## 上传 GitHub

请使用新导出的干净目录，而不是直接上传带论文与备份的工作目录：

```bash
python export_public.py --out GitHub发布包/campus-library-papers
```

导出文件包含源码、空白配置示例、截图、静态演示和测试；排除真实账号文件、用户配置、论文、清单、浏览器数据、备份与缓存，并生成文件哈希清单及 ZIP。

`.gitignore` 无法移除已经跟踪过的敏感文件。不要提交 `登录配置.private.json`；不要上传学校订阅论文。发布前由仓库所有者确定许可证；本次没有代选许可证、创建远程仓库或上传 GitHub。

## 目录

```text
campus_portal.py             本机配置服务（前台运行）
ui/                          HTML / CSS / JS
登录配置.example.json        空白公开示例
skill-src/                   论文获取 skill 与脚本
skill-src/tests/             离线回归
 docs/demo/                  无账号静态演示
 docs/images/                演示界面截图
export_public.py             公开发布包导出
```

更多数据处理说明见 [SECURITY.md](SECURITY.md)。
