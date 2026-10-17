# Fluent Check-in

<img src="checkin/assets/app.svg" width="96" alt="Fluent Check-in 图标">

Python 本地桌面签到工具，支持 AgentRouter / JustDoWork、独立 GitHub 会话和 Fluent 界面。

## 获取源码

```powershell
git clone https://github.com/SakuraForgot/FluentCheckin.git
cd FluentCheckin
```

本仓库只包含源码、图标及离线测试，不包含账号、会话、数据库或预置运行数据。软件与 GitHub、AgentRouter、JustDoWork 无官方关联；请按站点规则使用。

## 启动

主入口为 `main.py`。开发环境需要 Python 3.11+：

```powershell
python setup.py
.venv\Scripts\python.exe main.py
```

Windows 可双击 `start.cmd`。`python -m checkin` 保留为兼容入口。

```powershell
.venv\Scripts\python.exe main.py status
.venv\Scripts\python.exe main.py run
.venv\Scripts\python.exe main.py run --account <账号UUID>
.venv\Scripts\python.exe main.py gui --data-dir 'D:\MyCheckinData'
```

## 使用与请求控制

添加账号时直接在 GitHub 官方窗口登录，自动读取账号名。勾选站点后可执行当前账号或全部账号签到，过程显示在任务页；需要授权或验证码时使用“打开官网处理”。

- 账号会话相互隔离，网站身份不匹配时拒绝覆盖。当天有服务端明确签到成功记录则直接跳过；保存登录态、请求发出不等于到账。
- AgentRouter 每次实际操作结束后随机间隔 **30–60 秒**；JustDoWork 固定间隔 **60 秒**。两站独立计时，可取消，重启不清除间隔。
- HTTP 429 停止本轮该站后续账号，按有效 Retry-After 冷却，缺失时冷却 15 分钟；重复点击或手动入口不能绕过。
- 403、WAF、Turnstile 暂停该站自动任务，人工处理并确认成功后解除。其他站点可继续。
- AgentRouter 用明确成功标记或同账号当天 type=4 签到日志确认到账；日志查询异常不会触发重新登录。JustDoWork 用 checked_in_today 确认；未知结果不盲目重发 POST。
- 定时签到默认关闭，只在应用运行期间执行，不注册系统任务。

间隔用于减少集中请求，不保证免风控或代表站方允许批量领取；AgentRouter 官方公告禁止批量获取额度。

## 浏览器与 Windows 兼容范围

设置支持 **Microsoft Edge、Google Chrome、Brave**，以及单独安装的 Playwright Chromium。程序查找当前用户、Program Files、Program Files (x86) 下的标准位置和 Windows App Paths 注册项，不写死某台电脑的路径。

非标准安装可点击“选择浏览器程序”指定实际 EXE，每种浏览器分别保存路径；“恢复自动查找”清除自定义路径。找不到选定浏览器时明确提示，不自动切换到别的浏览器。所有浏览器均使用独立上下文，不读取日常浏览器用户目录。

调用的是完整浏览器，**只有 WebView2 Runtime 不够**。企业策略、旧版浏览器、便携启动器的参数转发可能影响自动化；自定义 EXE 不代表任意 Chromium 衍生版都兼容。Brave 使用 Chromium 自动化接口，不属于 Playwright 官方命名的 Edge/Chrome 通道。

本机构建为 Windows x64，分发基线按 Windows 11 x64 + 当前稳定版浏览器；Windows 10、ARM64 和其他设备需要另外实测，不宣称全 Windows 通用。[Playwright 系统要求](https://playwright.dev/python/docs/intro#system-requirements) / [品牌浏览器说明](https://playwright.dev/python/docs/browsers#google-chrome--microsoft-edge)。

默认包不捆绑浏览器，接收方需安装 Edge、Chrome 或 Brave。源码环境可另外安装 Chromium：

```powershell
.venv\Scripts\python.exe -m playwright install chromium
```

## Python 打包工具

```powershell
python setup.py --build
python build.py
# 可选单文件或控制台版本
python build.py --onefile
python build.py --console
# 原发布目录已运行并产生 data 时，另选干净输出位置
python build.py --output dist/release-new
```

脚本优先使用项目 .venv，以 main.py 为打包入口；PyInstaller 及 hooks 版本固定在 requirements-build.txt。默认输出：

```text
dist/onedir/FluentCheckin/FluentCheckin.exe
dist/onedir/FluentCheckin.zip
dist/onedir/FluentCheckin.zip.sha256
```

文件夹版需要分发整个目录或 ZIP，不能只复制 EXE。单文件版在 dist/onefile；控制台版名为 FluentCheckin-CLI，可用于 run/status 命令。构建会覆盖对应旧产物，构建前请关闭产物。中间文件和生成的 spec 在 build/。

如果目标目录已有运行数据，构建会停止并提示使用 `--output`，不会覆盖这份便携数据。应用图标源文件是 `checkin/assets/app.svg`；构建时自动用 `tools/render_icon.py` 生成 16–256 像素的 ICO，供窗口、任务栏和 EXE 使用。

包包含 Python、Qt 和 Playwright 驱动，不要求接收方安装 Python；不收集项目根目录、账号会话、测试数据和旧参考源码。[Playwright 打包说明](https://playwright.dev/python/docs/library#pyinstaller) / [PyInstaller 文档](https://pyinstaller.org/en/stable/usage.html)。

## 本地数据

默认采用便携结构：EXE 首次运行时在自身旁创建 `data/`；源码运行时在 `main.py` 旁创建。不会因为工作目录或单文件版的临时解压位置而改变保存位置，也不再使用 AppData 保存应用数据。

```text
FluentCheckin/
  FluentCheckin.exe
  _internal/                 # 文件夹版运行依赖
  data/                      # 仅运行后创建，发布包不包含
    app.sqlite3              # 账号、设置、历史、签到确认、间隔及冷却
    accounts/<UUID>/
      github.json
      agentrouter.json
      justdowork.json
    locks/
    desktop.lock
```

内部结构不变。发布包不带 `data`，不复制、迁移或自动导入旧账号数据；首次运行是空账号。整目录移动可携带本次使用产生的应用数据，整目录删除即可清除。该目录需要写入权限。显式使用 `--data-dir` 时按指定位置保存，这属于可选覆盖；浏览器本身及系统管理的临时文件不属于应用持久数据。

便携版验证：已在临时目录解压最终 ZIP，从不同工作目录启动 EXE，确认启动前没有 data，启动后仅在 EXE 旁创建空账号数据库；原始发布目录和 ZIP 均不带 data。没有迁移真实账号。

按用户要求不使用 DPAPI。明文会话具有登录权限，不要分享、上传或放入公开同步目录。不会保存密码、OTP、PAT 或模型 API Key；短期 Bearer 只在内存使用。日志不记录 Cookie、原始服务端错误和 OAuth 回调参数。移除账号只删除本地记录，不删除网站账号或撤销 GitHub 授权。

`github.json` 保存 GitHub 登录会话，两个站点的 JSON 分别保存各自会话；这些文件均属于敏感数据。提交 Issue 时仅提供脱敏的错误说明和复现步骤，不要上传 `data/` 或浏览器状态文件。

## 目录与测试

- main.py：主入口；setup.py：安装依赖；build.py：打包；start.cmd：双击启动。
- checkin/：界面、浏览器检测、会话、站点和任务逻辑。
- tests/：离线模拟与 UI 测试；requirements*.txt：运行和打包依赖。
- build/、dist/、artifacts/：生成文件，不提交版本库。

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -v
.venv\Scripts\python.exe -m tests.render_ui
```

浏览器测试使用离线模拟响应，UI 预览使用临时数据；真实授权、站点到账、次日恢复和其他设备兼容性需单独验证。

完整测试需要安装 Microsoft Edge；测试使用独立临时会话及模拟响应，不需要真实账号。GitHub Actions 在 Windows 上运行 Python 3.11 / 3.14 的离线测试，不执行真实签到。

2026-10-06 验证：源码及最终包内 Playwright 驱动均成功启动本机 Edge、Chrome、Brave 并执行离线页面检查；发布 EXE 使用隔离数据目录成功创建 Qt 主窗口。回归中固定等待导致的 UI 测试误报已改为状态同步，11 项 UI 测试重新通过。压缩包检查未包含账号、参考源码或测试目录。仅文件夹版完成实际构建验收；单文件/控制台为可选构建模式，未对其最终产物作同等验收。

实现参考：[newapi-ai-check-in](https://github.com/aceHubert/newapi-ai-check-in/tree/a01c232672686ce66ac06194a7f13b04ca734a28)、[checkin-agentrouter](https://github.com/jiang7f/checkin-agentrouter/tree/abe6438d0649c3e6efe354c99458cd6bcff76eb5)。运行时不执行参考项目代码。

## 开源许可

Copyright (c) 2026 SakuraForgot。项目源码及原创图标采用 [GNU GPL v3.0](LICENSE)（GPL-3.0-only）。第三方依赖及参考项目保留各自许可，见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
