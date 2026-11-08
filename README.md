# 三角洲行动特勤处助手（DeltaForce-SS-Auto）

> GitHub 仓库：DeltaForce-SS-Auto；本地可执行文件和计划任务仍为 `orc-sjz`。

Windows 桌面自动化工具：自动启动 WeGame → 三角洲行动 → 进入大厅/特勤处，读取四个工位的生产倒计时，按完成时间安排下一轮；同时支持 QQ/微信双账号、交易行子弹兑换、领取与制作、F12 取消、托盘常驻和无人值守。

> 当前版本：**v0.04**  
> Release：https://github.com/hebingking/DeltaForce-SS-Auto/releases/tag/v0.04  
> 源码：https://github.com/hebingking/DeltaForce-SS-Auto

> 本项目是个人使用的桌面自动化工具，不提供游戏修改、注入或破解功能。使用前请阅读文末免责声明。

---

## 功能

- **完整启动链路**：启动 WeGame、打开三角洲行动详情页、点击启动、进入烽火地带/大厅、打开特勤处。进入游戏后若出现全屏活动页，会自动识别底部中央的「⎵ 继续」提示并先按空格，再继续 Tab/大厅流程（不依赖会变化的活动名称）
- **双账号**：QQ / 微信顺序切换，通过账号按钮 OCR 定位，不依赖固定坐标；进入游戏后读取 UID 校验，防止用错账号执行
- **倒计时读取**：读取技术中心、工作台、制药台、防具台四个工位的剩余时间
- **自动排程**：按 10 分钟分组规则计算下一次启动时间；支持合并窗口、两种多账号模式、补跑、锁屏重试
- **领取与制作**：领取已完成奖励，按配置制作物品；可设置一键补齐总价上限（**先跑领取/制作，再决定要不要进交易行**）
- **交易行兑换**：搜索子弹、识别兑换按钮状态和折扣；低于阈值/置灰/当日已完成时跳过；支持军需处兑换、一键补齐和确认。当日子弹已全部兑换/置灰时**直接跳过，不进交易行**；兑换结束后会退出物品详情页并返回特勤处
- **F12 取消**：流程运行中按 F12 不再发送后续点击/按键，当前流程在下一个检查点退出
- **结束关闭 WeGame**：可选；结束后延迟 N 秒正常关闭 WeGame 窗口，具体是否最小化到托盘由 WeGame 自身设置决定
- **托盘 / 配置界面**：读取、立即执行、取消、暂停/恢复、查看下次时间、打开日志、环境检查
- **无人值守**：登录自启、最高权限计划任务、运行期间阻止系统睡眠（Windows 自带自动登录需你本人配置）

---

## 环境要求

- Windows 10 / 11 x64
- 已安装 WeGame 和三角洲行动
- 需要管理员权限（WeGame / 游戏需要）
- 源码运行：Python 3.13（3.14 的 OCR/onnxruntime 轮子可能不完整）
- 依赖见 [`requirements.txt`](requirements.txt)

---

## 下载与安装（exe）

1. 从 [v0.04 Release](https://github.com/hebingking/DeltaForce-SS-Auto/releases/tag/v0.04) 下载 `orc-sjz.exe`
2. 将仓库里的 [`config.example.json`](config.example.json) 复制为同目录的 `config.json`
3. 打开 `config.json`，至少确认：
   - `wegeame_exe`：你的 WeGame 安装路径
   - `game_process_hint` / `game_title_hint`
   - `accounts.items` 里各账号在 WeGame 账号选择器中显示的名称/号码
4. 右键 `orc-sjz.exe` → 以管理员身份运行
5. 首次运行可能触发 SmartScreen（exe 未签名）：更多信息 → 仍要运行

运行后程序在托盘常驻，主界面可保存配置、立即执行、取消当前流程和查看日志。

> Release 里的 `orc-sjz.exe` 为 v0.04 构建，SHA-256：`754405c97d5707182a6e1660ffe8b3309e2cc60503905de04bb27a9d119f7119`。

---

## 从源码运行

```powershell
cd orc-sjz
py -3.13 -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

打包：

```powershell
.\.venv\Scripts\pyinstaller.exe --noconfirm orc-sjz.spec
# 输出：dist\orc-sjz.exe
```

`orc-sjz.spec` 已配置单文件、无控制台、内嵌管理员清单和图标。

---

## 配置说明

配置文件为 `config.json`（不要提交到公开仓库）。常用字段：

| 字段 | 说明 |
|---|---|
| `wegeame_exe` | WeGame 可执行文件路径 |
| `wegeame_process_hint` | WeGame 进程/路径特征，用于找窗口 |
| `game_process_hint` / `game_title_hint` | 三角洲进程名和标题特征 |
| `stations` | 四个工位名称 |
| `exit_game_after_read` | 读取完成后自动退出游戏，默认开启 |
| `close_wegame_after_run` | 完整流程结束后关闭 WeGame 窗口 |
| `close_wegame_delay_seconds` | 关闭前等待秒数，默认 5；0 为立即关闭 |
| `prevent_sleep` | 程序运行期间阻止 Windows 系统睡眠 |
| `catch_up_hours` | 错过的计划点在多少小时内补跑，默认 24 |
| `lock_retry_minutes` / `lock_retry_max` | 锁屏时重试间隔和次数，默认 5 分钟 × 24 次 |
| `accounts.items[]` | 账号列表：`key`、`label`、`matches`、`uid`、`enabled` |
| `multi_account_mode` | `per_account`（各排各的）或 `both`（两个都跑） |
| `schedule_merge_minutes` | 两个账号启动时间接近时合并的窗口，默认 10 分钟 |
| `exchange.enabled` / `min_discount_percent` / `items` | 交易行兑换开关、最低折扣和子弹列表 |
| `craft.enabled` / `max_purchase_total` / `items` | 领取制作开关、一键补齐总价上限、物品配置 |
| `rule.group_threshold_minutes` / `buffer_seconds` | 倒计时分组阈值和启动缓冲 |
| `ocr` / `timeouts` / `poll` / `notify` | OCR 后端、各阶段超时、轮询间隔、提示音 |
| `ocr.max_dimension` / `native_upscale` | 精细识别（读取工位倒计时）用的尺寸上限和放大倍数 |
| `ocr.fast_max_dimension` / `fast_upscale` | 交易行兑换高频轮询用的尺寸上限和放大倍数；默认 3000 / 1.0（即不缩放），调小更快、调大更准 |
| `ocr.fast_det_limit` | 快读引擎的检测长边上限，默认 1152。只影响兑换轮询，**不影响工位倒计时识别精度** |

### 账号匹配与 UID

`accounts.items[].matches` 填 WeGame 账号选择器里显示的名称或号码，例如 `["QQ", "123456789"]`、`["微信", "昵称"]`。  
首次成功进入游戏后程序会读取并记录该账号的 UID 到 `accounts_state.json`（本机文件，不要提交），之后自动校验，避免切换失败时用错账号。

---

## 使用方式

### 主界面

- **保存配置 / 重新加载 / 打开配置文件**：修改后保存生效
- **立即读取倒计时**：游戏已在特勤处时直接读取
- **立即执行**：按选择的账号和模式跑完整流程
- **取消当前运行(F12)**：只取消当前流程，程序继续留在托盘
- **暂停/恢复自动启动**：暂停定时任务，不影响正在运行的流程
- **环境检查**：检查 WeGame/游戏/特勤处页面和 OCR 基础识别

### 托盘菜单

- 打开主界面
- 立即执行完整流程
- 取消当前运行 (F12)
- 查看下次自动启动
- 暂停/恢复自动启动
- 打开日志
- 退出

### 两种排程模式

- **各排各的**：每个账号按自己的时间点执行
- **两个都跑**：每次启动同时执行勾选账号，共用一次启动链路

时间相近（默认 10 分钟内）的点会合并成一个任务；启动时间冲突时，后到的任务会排队。

### F12 取消

流程运行中按 **F12**：

- 立即停止发送新的点击/按键
- 当前流程在下一个检查点退出
- 游戏和 WeGame 保持当前状态，不自动关闭
- 排程保留，排队任务清空
- 取消是协作式的：如果某一击已经发出（例如购买确认），无法撤回

F12 使用全局按键轮询，不注册全局热键，因此不会抢走游戏对 F12 的接收；如果游戏本身也用 F12（例如截图），可能同时触发游戏动作。

### 结束关闭 WeGame

勾选「结束后关闭 WeGame 窗口」后，在一次完整流程（至少一个账号成功）结束后等待配置的秒数，再向 WeGame 主窗口发送正常关闭消息：

- WeGame 若设置为关闭时最小化到托盘，会按它的设置最小化，程序不会强杀进程
- 等待期间按 F12 会取消这次关闭，WeGame 保持打开
- 全部账号失败时不关闭

---

## 无人值守提示

程序需要**已登录且未锁屏的桌面**才能操作游戏。完整无人值守还需要：

1. Windows 自动登录（需要你本人配置；程序不保存密码）
2. 「登录时」触发、最高权限的计划任务指向 `orc-sjz.exe`
3. 睡眠/息屏/锁屏策略允许程序运行
4. 不要在无人值守时合盖（笔记本合盖可能进入睡眠）

仓库提供只读检查脚本 [`check_autostart_env.ps1`](check_autostart_env.ps1)，可检查屏保、自动锁屏、动态锁、睡眠、自动登录和 orc-sjz 任务状态。它默认不修改任何设置。

---

## 目录结构

```text
orc-sjz/
  app.py                 程序入口/托盘/日志
  gui.py                 配置与状态界面
  launcher.py            WeGame→游戏→大厅→特勤处
  reader.py              窗口识别/OCR/倒计时解析
  scheduler.py           排程、合并、补跑、锁屏重试
  exchange.py            交易行兑换
  workshop.py            领取与制作
  accounts.py            账号匹配与 UID 学习
  planner.py             倒计时→下次启动时间
  winutil.py             Win32 输入/窗口/截图
  ocr_backend.py         RapidOCR / Windows OCR
  cancel.py              取消标记
  hotkey.py              F12 轮询
  envcheck.py            环境检查
  autodetect.py          路径/进程自动获取
  calib.py               标定辅助
  assets/icon.ico        图标
  orc-sjz.spec           PyInstaller 配置
  check_autostart_env.ps1 无人值守只读检查
  config.example.json    脱敏配置模板
```

---

## 常见问题

**启动提示找不到 config.json**  
把 `config.example.json` 复制为 `config.json`，并修改 WeGame 路径。

**找不到 WeGame 或游戏窗口**  
确认 `wegeame_exe`、`wegeame_process_hint`、`game_process_hint` 正确；可在界面里用自动获取或手动填写。

**账号切换失败 / UID 不匹配**  
检查 `accounts.items[].matches` 是否包含 WeGame 账号选择器中实际显示的名称或号码；删除 `accounts_state.json` 中对应 UID 可重新学习。程序在 UID 不匹配时会退出游戏，不会继续操作错误账号。

**兑换按钮是灰色**  
表示已兑换或暂不可用，程序会跳过并记录；当日已完成的物品后续会直接跳过。

**折扣识别为未知 / 低于阈值**  
确认游戏内显示「预估兑换成本」和折扣百分比；阈值在 `exchange.min_discount_percent` 配置。

**F12 没反应**  
F12 只在托盘程序运行且当前流程忙时生效；如果日志没有「已请求取消」，说明按键没被检测到，可换一个按键或使用托盘菜单「取消当前运行」。

**提示锁屏跳过 / 长时间未执行**  
程序检测到锁屏会每 5 分钟重试，最多 24 次；请检查 Windows 的锁屏/息屏策略，或使用环境检查脚本。

**首次运行被 SmartScreen 拦截**  
exe 未签名，点「更多信息 → 仍要运行」；运行需要管理员权限。

**OCR 识别不稳定**  
确保游戏分辨率/DPI 与运行环境一致；OCR 默认 RapidOCR，可在 `config.json` 的 `ocr` 中调整后端和放大倍数。

**交易行兑换太慢**  
兑换流程默认用 `ocr.fast_*` 低分辨率参数，并且只识别卡片区/弹窗区（不是整屏）。如果觉得慢，可以调小 `ocr.fast_det_limit`（默认 1152，调到 960 更快但小字更容易错）；如果出现折扣数字或物品名识别不到，把它调到 1536~2048 再试。日志里每颗子弹都有 `耗时 x.xs`，方便对照。

---

## 隐私说明

公开仓库不包含真实 `config.json`、`accounts_state.json`（UID）、`exchange_history.json`、`state.json`、日志或本机截图。  
这些文件已在 `.gitignore` 中排除；请勿把它们提交到公开仓库。程序不保存 WeGame 密码，自动登录需由你自行在 Windows 中配置。

---

## 免责声明

本项目仅用于个人电脑上的重复性界面操作自动化，不修改游戏文件、不注入进程、不提供游戏优势。  
使用自动化可能违反游戏/平台的服务条款，存在账号限制或封禁风险；请自行评估并承担使用后果。游戏更新可能导致界面/OCR 失效，项目不保证持续可用。
