# M4 分区模式设计 — 每排一任务 × Codex 接入

> 状态：**用户已拍板（2026-10-14）**。本文是实施前的最终设计。协议层（rk98.py / keymap.py 帧结构）零改动，M4 只改"谁上什么色"的决策层。

## 0. 与 M1–M3 的关系

- M1–M3 交付的全局模式（彩色流水 / 红灯流水 / 绿灯闪 / 停流回硬件）**完整保留**，作为可配置回退。
- M4 新增：任务 → 排槽位分区渲染 + Codex 信号源 + 托盘任务映射表。
- 关键事实（来自 keymap.py 实测推导，**修正此前口误**）：
  - 主键区 6 排 = **19 / 14 / 14 / 13 / 13 / 10** 键（共 83）；
  - numpad = **17 键**（不是 18），按网格 x 坐标分半：**左半 10 键 / 右半 7 键**。

## 1. 编码规则（已确认）

两个正交维度：**颜色 = 来源，模式 = 状态**。比"颜色=状态"更优——加第三种 agent 只需加一个颜色。

| 任务状态 | WorkBuddy 任务 | Codex 任务 |
| --- | --- | --- |
| 运行中 | 绿色流水（排内） | 蓝色流水（排内） |
| 待确认 | 红色闪烁·持续到确认 | 同左 |
| 完成 | 绿色闪烁 ×N（次数/亮灭 ms 面板可调，沿用现有 done 配置） | 同左（完成是通用终止态，覆盖来源色） |
| 失败（Codex `turn.failed`） | —（WB 无此态） | 红色闪烁·持续，`fail_flash_secs`（默认 30s）后自动释放 |

- 色盲友好：红闪（一直闪）vs 绿闪（闪 N 次后停）靠**时序**区分；蓝/绿流水靠色相 + 所在排区分。
- 绿的双重身份（运行=绿流 / 完成=绿闪）靠**流→闪的模式切换**表达完成信号，可接受。

## 2. 键位分区

主键区 6 排 = 6 个任务槽；numpad 17 键 = 溢出区，左右分半。

| 槽位 | 物理排（网格 y） | 键数 | 内容 |
| --- | --- | --- | --- |
| P1 | y=0 | 19 | Esc / F1–F12 / PrintScreen / Del / Home / Insert / PageUp / PageDown |
| P2 | y=2 | 14 | ` 1…0 -_ += Backspace（不含 numpad） |
| P3 | y=4 | 14 | Tab Q…P [ ] \ |
| P4 | y=6 | 13 | Caps A…L ; ' Enter |
| P5 | y=8 | 13 | Shift Z…M , . / RShift Up |
| P6 | y=10 | 10 | Ctrl Win Alt Space Alt Fn RCtrl ← ↓ → |

**溢出区**（numpad 17 键，按 x 坐标切两半）：

- 左半（x ≤ 30，**10 键**）= Codex 溢出：NumLock Num7 Num4 Num1 Num0 Num/ Num8 Num5 Num2 Num.
- 右半（x ≥ 32，**7 键**）= WorkBuddy 溢出：Num\* Num9 Num6 Num3 Num- Num+ NumEnter

溢出语义：**颗数 = 溢出任务数**（封顶 10/7，超出显示满半区）；该半区按内部最高优先级状态动画（等确认 > 失败 > 完成 > 运行）；无溢出时全暗。溢出即降级——不区分个体，只看颗数和紧急度。

## 3. 分区状态机

### 3.1 任务记录

`task_id`（WB: session_id；Codex: thread_id）→ TaskRec：

| 字段 | 说明 |
| --- | --- |
| `source` | `"workbuddy"` / `"codex"` |
| `state` | `RUNNING` / `WAITING` / `DONE` / `FAIL` |
| `slot` | 0..5，分配后**稳定不换排** |
| `progress` | `(done_steps, total_steps)` 或 None；另有 `steps` 活动度计数 |
| `label` | cwd / 首条用户消息（面板显示用，键盘上不可见） |
| `last_seen` | 心跳时间戳（僵死清理用） |
| `fail_until` | FAIL 自动释放时刻 |

### 3.2 排分配 / 回收

- **分配**：任务创建时取**最小空闲槽位**（视觉顺序 = 创建顺序，先到先得）。
- **稳定**：任务存活期间槽位不变；中间槽释放**不触发重排**（空出的槽才被新任务使用）——避免视觉跳动。
- **回收**四条路径：
  1. `Stop` / `turn.completed` → **DONE**（整排绿闪 ×N）→ 播完释放槽位；
  2. `SessionEnd` / thread 关闭 → **直接释放，不闪绿**（会话关闭不算完成）；
  3. 僵死（`stale_session_secs` 无心跳，janitor 清理）→ 直接释放，不算完成；
  4. FAIL → `fail_flash_secs` 后释放。
- **溢出**：无空闲槽且新任务到来 → 进对应来源的溢出半区。

### 3.3 停流时机

- 所有槽空闲 **且** 两个溢出半区全暗 → 立即停流，键盘回硬件灯效（点击扩散）。
- 最后一项任务的绿闪播完才停（绿闪期间持续推流）。
- **无"全场绿闪谢幕"**——每个任务完成只闪自己那排（用户最终拍板，取代早期方案）。

## 4. 渲染规则

每帧合成一张 100 键全帧下发。所有分区共享同一帧缓冲，6 排同时渲染**零额外带宽**（与全局模式同为 8 个 Feature 报表/帧）。

| 任务状态 | 该排渲染 |
| --- | --- |
| RUNNING（有 plan） | **亮格内**跑来源色流水（亮格数 = 完成步骤/总步骤，从左到右填），未亮格暗 |
| RUNNING（无 plan） | 整排来源色流水，不填充（活动度无总量，填充即假进度） |
| WAITING | 整排红闪，持续；进度冻结，确认后回到原进度继续 |
| DONE | 整排绿闪 ×`done.times`（亮灭用 `done.on_ms/off_ms`），播完释放 |
| FAIL | 整排红闪，持续到自动释放 |

- **排内流水**：波前只在该排的 x 范围扫描（不是全键盘 37 列），观感 = 波浪沿这一排跑。
- **进度来源两档**：
  - WB：`PreToolUse` 且 `tool_name=TodoWrite` → 解析 `tool_input.todos` 的 status → 完成/总数；
  - Codex：rollout 中 plan 类 `item.completed` → 完成/总数（schema 待 E3 核对）；
  - 都没有 → 只流水不填充。

## 5. Codex 信号源

### 5.1 三源优先级

1. **rollout JSONL 尾随（主力，全版本可用）**：`~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`，追加式事件流，信息量比 hook 更大；
2. **notify hook（补充）**：`~/.codex/config.toml` 根级 `notify=[...]`，**仅 turn 完成**时触发，payload 带 thread-id / turn-id / cwd / 最后消息——做"完成确认"和会话身份绑定的备份通道；
3. **hooks.json（可选）**：需 `[features] hooks=true` 且首次手动信任，事件名与 WB 几乎一致——版本够就用，不够不强求。

### 5.2 rollout 事件 → 内部信号映射（**E3 已实测定稿**）

以下为 2026-10-14 凌晨对本机 12 个最新 rollout 文件（codex-cli 0.162.0-alpha.17.2）的全量词汇表聚合结果，取代此前的文档假设：

| rollout 事件（实测存在） | 内部信号 | 效果 |
| --- | --- | --- |
| `session_meta`（文件首行） | TASK_START(codex) | 建任务、分配槽位；payload 带 `session_id` / `cwd` / `cli_version` |
| `event_msg/task_started` | HEARTBEAT | 刷新心跳（一个 turn 开始） |
| `event_msg/item_completed`（`item.type` ∈ CommandExecution / AgentMessage / McpToolCall / Reasoning / UserMessage / Extension） | HEARTBEAT（steps+1） | 步数活动度；CommandExecution 计为有效工作步 |
| `event_msg/task_complete` | TASK_END | DONE 绿闪（payload 带 `turn_id` + `last_agent_message`，可做面板标签） |
| `event_msg/turn_aborted` | TASK_FAIL | FAIL 红闪（**真实的失败事件名，不是 `turn.failed`**） |
| （无对应事件） | TASK_CLOSE | 会话停滞超 `stale_session_secs` → janitor 直接释放，不闪绿 |

**实测不存在的三类事件（设计假设被推翻，相应能力裁剪）**：

1. **审批类事件完全不在 rollout 里**（桌面版审批在 App 内处理，不落盘；聚合的 "approval" 命中全是 `approval_policy` 配置字段）→ **Codex 没有 WAITING 信号源**。唯一途径是 `~/.codex/hooks.json` 的 `PermissionRequest` 事件，但该文件已被第三方工具 Agent Studio 占用——追加 hook 组（append-only、非破坏）列为**可选后续，默认不做**；
2. **没有 plan 类事件** → Codex 进度只有步数活动度，**不做亮格填充**（"有 plan 填充"仅适用于 WB 的 TodoWrite）；
3. **`notify` 通道本机不可用**：`~/.codex/config.toml` 的 `notify` 已指向 Codex 桌面版自己的 computer-use 组件，无法追加我们的转发器 → **rollout 尾随是唯一信号源**（本来也是主力，影响可控）。

**其它实测事实**：`~/.codex/sessions` 下 140 个 rollout **全部明文，无 .zst 压缩**（5 个月前的文件仍可读）→ 压缩处理降级为防御性分支；存在 `archived_sessions/` 目录（用户归档的会话），尾随只盯 `sessions/` 活跃目录；一个文件 = 一个会话（多 turn 追加在同一文件），子 agent 会话（如 guardian 审查）有独立文件和 `parent_thread_id`。

**信号归一化层**：`Service.apply_signal(source, task_id, kind, extra)` 为唯一入口。WB 侧现有 `handle_event` 把 9 个 hook 事件映射成同一组信号（现有逻辑改为调用 apply_signal，行为不变）；Codex 侧由 codex_tail 映射。两条源共用一套槽位状态机。

### 5.3 尾随容错（codex_tail.py）

- **torn line**：只解析以 `\n` 结尾的行，残行留缓冲等下一次读；
- **文件发现**：每 2s glob 今天 + 昨天两个目录；记录已见文件路径；新文件延迟 1s 从头读（防半写 header）；
- **压缩轮转**：7 天前文件变 `.jsonl.zst`——原路径消失即标记关闭，**跳过 .zst 不解析**；
- **`--ephemeral`**：不落盘 → 仅 notify 兜底（进度丢失，至少完成态不丢），文档明示此限制；
- **服务重启**：新文件从头读、旧文件只读尾部新增；旧任务不补渲染（键盘是实时指示器，可接受）。

### 5.4 notify 接入（可选通道）

`~/.codex/config.toml` 追加（先备份）：

```toml
notify = ["<python.exe>", "<rk98-keylight>/src/codex_notify.py"]
```

`codex_notify.py`：读 stdin JSON → 裸 socket POST `127.0.0.1:18990/event`（复用 hook.py 的发送方式：绕开 HTTP_PROXY、1s 超时），payload 带 `source="codex"`。仅作 `turn.completed` 的备份通道。

## 6. 配置与面板

### 6.1 config.json 新增段

```json
"partition": {
  "enabled": true,
  "fail_flash_secs": 30,
  "waiting_mode": "flash",
  "waiting_on_ms": 150,
  "waiting_off_ms": 100,
  "overflow": { "codex": 10, "workbuddy": 7 }
},
"sources": {
  "workbuddy": { "color": "#00E676" },
  "codex":     { "color": "#2979FF" }
},
"codex": { "enabled": true, "sessions_dir": "~/.codex/sessions", "poll_secs": 2 }
```

- `running` / `waiting` / `done` 现有段**全部保留**：全局回退模式继续用；分区模式直接复用 `done` 的次数/亮灭 ms 和 `running` 的速度/帧率/亮度；
- `partition.enabled=false` → 一键回退现有全局模式（托盘复选框控制）。

### 6.2 托盘面板改动（tray.py）

- 新增"分区模式"框：启用开关 / WorkBuddy 运行色 / Codex 运行色 / 失败闪烁秒数 / **"演示分区"按钮**（注入 WB×2 + Codex×1 三个假任务跑 30s，不依赖真实 agent 即可验证观感）；
- 新增**任务映射表**（只读，0.5s 刷新）：排号 | 来源 | 状态 | 进度 | cwd/标签——键盘上显示不出的文字身份在这里补足；
- 状态栏扩展：分区模式下显示"进行中 N/6 排 · 溢出 c/w"；
- 现有控件全部保留（全局模式仍可配可测）。

## 7. 代码改动清单

| 文件 | 改动 | 预估 |
| --- | --- | --- |
| `src/keymap.py` | 导出 `ROWS` / `NUMPAD_LEFT` / `NUMPAD_RIGHT` 常量（纯数据推导，协议不动） | +25 行 |
| `src/effects.py` | 排内流水波、亮格填充、排级红/绿闪帧 | +70 行 |
| `src/partition.py`（新） | 任务表 + 槽位分配器 + 帧合成器 | ~220 行 |
| `src/keylight.py` | `apply_signal` 归一化；任务表；Renderer 分区分支；`/status` 扩展；DEMO 注入 | ~160 行 |
| `src/codex_tail.py`（新） | rollout 尾随 + 容错 + 事件映射 | ~190 行 |
| `src/codex_notify.py`（新） | notify 转发（可选备份通道） | ~45 行 |
| `src/tray.py` | 分区设置框 + 任务映射表 + 演示按钮 | ~130 行 |
| `config.json` | 新段（默认分区开） | +30 行 |

合计约 870 行，**协议层 0 行**。

## 8. 验证计划（E1/E2/E3 已于 2026-10-14 凌晨执行完毕，结果见第十节）

~~先做三个探针再动代码~~ → **已完成**：

- ~~**E1 zone 局部推送**~~ → 已测：无读回通道无法判定生效性；耗时 1.63x 但瓶颈不在传输 → **维持全帧推送，优化存档**；
- ~~**E2 单排渲染**~~ → 已测：30fps 节流精确达成、无节流 42fps；**实测抓出网格阈值推导会误杀导航键的 bug**，已修为 keymap.py 显式常量 + assert；
- ~~**E3 Codex 环境**~~ → 已测：CLI 0.162.0-alpha.17.2 在装且活跃；事件词汇表实测定稿（见 5.2）；审批/plan/notify 三项能力按实测裁剪。

**新的验证策略（因设备无读回而确立）**：灯效"是否生效"无法从设备侧回读，改为三层——

1. **离线单元测试（主力）**：对分区帧合成器做确定性断言——给定任务表，断言产出的 100 键颜色数组逐键符合预期（哪排亮、亮什么色、溢出半区颗数、暗键为 (0,0,0)）。合成逻辑是纯函数，可 100% 机器判定；
2. **写入 ACK**：`send_feature_report` 每包返回 65 字节即被 HID 驱动真实接收（现有错误处理已覆盖重连）；
3. **用户终验**：托盘"演示分区"按钮注入假任务，用户方便时一眼确认观感。

端到端验证顺序：单元测试 → 演示模式注入假任务 → 真实 WB 任务上排 → 真实 Codex 任务上排（尾随真实 rollout）→ 完成绿闪释放 → 溢出演示（注入 8 个任务）→ 停流回硬件。

## 9. 风险

1. ~~**E3 可能推翻 schema 假设**~~ → **已发生并定稿**：审批/plan/notify 三项不存在或不可用，Codex 信号收敛为 rollout 单源，映射表见 5.2；
2. **单排 10~19 键的流水观感**可能比全键盘稀薄——单元测试只能验逻辑，观感待演示按钮人工确认；参数（波尾 tail、速度）已在配置里可调；
3. **等待红闪整排常闪**可能干扰打字视线——`waiting_on/off_ms` 可调，默认 150/100ms 偏快偏醒目；
4. **设备无读回通道**（E0 实测）：任何"灯效是否正确"的设备侧自动判定都不可行，只能靠离线单元测试 + 写入 ACK + 人工终验三层兜底；
5. **Codex 无 WAITING 信号**：桌面版审批不落盘，Codex 任务等待确认时键盘无红闪提示（WB 侧不受影响）。可选后续：向 `~/.codex/hooks.json` append PermissionRequest hook 组（Agent Studio 已占用该文件，需非破坏式追加，默认不做）。

## 十、探针实测记录（2026-10-14 00:45–01:00，无人值守，服务暂停 5 分钟后已恢复）

探针脚本：`probes/hw_probe.py`（readback / single-row / zone-partial 三个子命令）。执行时停止托盘服务独占 HID，结束后重启并验证 `/status` 正常（`connected: true`）。

### E0 读回能力 — **不可用（定论）**

- `get_feature_report(0x0A, 65)` 不报错，但回传固定模式 `01 01 01 01 01 00 00 00` 循环，**发送任意帧前后内容完全一致**；
- 扫描报表 ID 0x00–0x30：只有 0x0A 有响应，且内容静态。
- 结论：**RK98 灯效状态只写不可读**。所有"是否生效"的验证改走离线单元测试（第 8 节）。

### E1 zone 局部推送 — **不启用（存档）**

- 生效性无法判定（无读回）：只推 zone3+commit 是否被设备应用，机器不可验证；
- 耗时实测：全帧 8 报表 **23.56 ms/帧**（理论 42fps）vs 局部 4 报表 **14.49 ms/帧**（理论 69fps），加速 1.63x；
- 但真实瓶颈是**帧生成**（服务端彩虹帧 ~12fps vs 裸写 42fps，差 3.5 倍在 Python 计算），局部推送省的传输时间杯水车薪；
- **决策：维持全帧推送（现网验证过的路径），1.63x 优化存档**，待用户视觉确认有新需求再捡起。

### E2 单排渲染 — **通过，并抓出一个真 bug**

- 30fps 节流精确达成（150 帧/5.0s），无节流 42fps——**协议层余量充足，分区模式带宽无忧**（6 排共享一帧，与全局模式同价）；
- **抓 bug**：初版行推导用网格阈值 `x>=27` 剔除 numpad，把第 1 排导航键（Del/Home/Insert/PageUp/PageDown）误杀（P1 算成 14 键）；改用 `y>=2` 叠加后仍错——根因是**第 2 排 Backspace 的网格 x=27 与 numpad 相邻**，网格坐标布局本身不规整；
- **修复**：`keymap.py` 增加显式常量 `ROWS`（19/14/14/13/13/10）/ `NUMPAD_LEFT`（10 键）/ `NUMPAD_RIGHT`（7 键），全部带 assert 自检（100 键覆盖完整、分半不重不漏），探针改引用常量。**这类错误正是无人值守验证的价值：在写实现前拦下。**

### E3 Codex 环境 — **通过（情报见 5.2）**

- codex-cli 已安装（桌面版，rollout 会话记录在用户目录 ~/.codex/sessions），用户在活跃使用；
- 事件词汇表实测聚合（12 文件）：`session_meta` / `event_msg/{task_started, item_completed, task_complete, turn_aborted, token_count, thread_settings_applied}` / `response_item/{message, reasoning, function_call, custom_tool_call...}` / `turn_context` / `token_usage_record` / `world_state`；
- `item_completed` 的 `item.type` 实测：Reasoning / CommandExecution / AgentMessage / UserMessage / McpToolCall / Extension；
- **无审批事件、无 plan 事件、notify 被占用、无 .zst 压缩**——四项能力按实测裁剪（详见 5.2）。

### 工程量台账更新

| 项 | 状态 |
| --- | --- |
| `keymap.py` ROWS/NUMPAD_LEFT/NUMPAD_RIGHT + assert | **已完成**（探针期间落地） |
| `probes/hw_probe.py` 三探针 | **已完成** |
| `src/effects.py` 排级帧函数（row_phase/row_flow_map/lit_row_flow_map） | **已完成** |
| `src/partition.py` 帧合成器 + 状态机 | **已完成** |
| `tests/test_partition.py`（16 用例，帧合成逐键断言） | **已完成，16/16 通过** |
| `src/keylight.py` apply_signal 归一化 / 分区渲染 / janitor / /status / /test DEMO | **已完成** |
| `src/codex_tail.py` rollout 尾随 + 容错 | **已完成** |
| `tests/test_codex_tail.py`（5 用例，含真实样本严格对账） | **已完成，5/5 通过** |
| `src/tray.py` 分区设置框 / 任务映射表 / 演示按钮 | **已完成** |
| `config.json` partition/sources/codex 新段 | **已完成**（分区默认开） |

**实施期实测抓到的 2 个 bug（均被离线测试拦下）**：

1. `task_complete`/`turn_aborted` 的 payload **不带 thread_id**（只有 turn_id）→ 完成/失败信号被 tid 空值丢弃。修复：tailer 按文件记住最近活跃 thread_id（一个文件=一个会话），完成信号落到当前活跃线程；
2. 测试侧口径问题若干（进度边界重叠、fail_flash 0.5s 钳制、防半写 1s 延迟）——非产品 bug，但逼得把边界语义写清楚。

**端到端验证记录（2026-10-14 09:10–09:14，无人值守）**：

- 服务以分区模式重启：`/status` 返回 `partition:true, connected:true`；
- 真实 WB hook 自动上排：当前对话会话占 slot 0（steps 随工具调用增长）——证明 WB 侧归一化链路真实工作；
- 演示分区（3 任务）：slot 1/2/3，codex 任务 WAITING（全局态 WAITING，红闪），6s 到期全部走完 绿闪→释放；
- 演示溢出（10 任务）：6 上排 + Codex 溢 3 / WB 溢 2，颗数与优先级符合设计；
- 精确停流证明：清除任务后 frames 冻结（A=B=789，state=IDLE，tasks=0）；
- Codex 尾随线程启动正常，旧文件正确识别为"只读尾部"；无真实 codex 活动时无信号（设计原则）。

## 十一、线上迭代记录（2026-10-14 11:00–11:50，用户报障驱动）

### 11.1 修复：没有待确认状态却一直红闪（NOTICE 脉冲）

**报障**：用户无待确认操作，第一个任务整排红闪不灭。

**根因**：`_handle_partition` 把 `Notification` 映射为持久 `ASK`。但 WorkBuddy 的 Notification 在**每轮回答结束、空闲等输入时都会触发**，不是"需要拍板"；真正的待确认信号是 `PreToolUse(AskUserQuestion)`。分区模式下 ASK 只能被下一次工具调用或 Stop 清除，于是每轮结束都红闪直到下一个事件。全局模式本来有 `notice_until` 20s 过期保护，分区改造时漏了这个语义。

**修复**：`partition.py` 新增 `NOTICE` 瞬时脉冲信号（`wait_until` 截止，到期由 janitor 转 DONE 绿闪释放，默认 20s）；不打断持久 ASK；`tests/test_partition.py` 补 5 个脉冲用例（全生命周期/不被心跳误清/不打断 ASK/DONE 不受干预/到期转释放）。在线密集采样验证：T+2~19s 红闪 → T+23s 绿闪 → T+26s 释放；持久 ASK 26s 后仍在等、答复即恢复。

### 11.2 新增：重新扫描当前任务（rescan v2）

**需求**：用户测试灯效（演示/清空）后，没有选项重新抓取当前任务状态。

**数据源决策（关键）**：第一版实现为重放 `hook-events.jsonl` 全量历史，在线实测 **restored=0**——该文件只旁路记录 `Stop`/`FinalStop`（126 条全是这两类，没有 UserPromptSubmit/PreToolUse 等建立事件，那些走 HTTP /event 直连），重放重建不出任务。改用 **WorkBuddy 会话记录** `<projects>/*/*.jsonl`：文件名 = session_id，文件 mtime = 最后活动时间；`projects` 根目录从 hook 事件的 `transcript_path` 字段推导（不硬编码个人路径，可用 `source.projects_root` 显式指定）。

**语义**：mtime 在 `stale_session_secs`（600s）内 = 活跃会话 → 建任务；Stop/FinalStop 只表示"一轮回答结束"，不阻止恢复——用户点扫描就是要抓"当前还在的对话"；僵死会话不恢复。入口三处：托盘右键"重新扫描当前任务"、设置窗分区框内按钮、HTTP `action=RESCAN`。在线实测 restored=1（当前对话）。

### 11.3 新增：分区/全局双模式切换入口

设置窗原有"启用分区模式"开关之外，托盘右键加动态菜单项"切换: 分区(每排一任务) → 全局"（文字随当前模式变化），HTTP `action=TOGGLE_PARTITION` 同步支持。切换即清空当前模式状态、按新模式自动重扫历史恢复真实任务。在线实测：分区→全局（state 由 sessions 推导）→分区（任务自动恢复）全链路通过。

### 11.4 修正：亮灯逻辑图示渲染错误（docs/lighting-logic.svg v2）

用户截图报障 GitHub 上图显示有误。核对线上文件与本地字节一致后定位为 **SVG 自身布局缺陷**（非版本问题）：

1. 排说明文字过长，与右侧 numpad 溢出区格子重叠（排1"（亮格 = 已完成步骤）"被 numpad 白格盖住）；
2. numpad"WorkBuddy 溢出 →"用 `text-anchor="end"` 定位在格子右缘，文字向左延伸约 110px 压在第一列格子上；
3. numpad 第 5 行 48px 宽格横跨 Codex/WB 两半边界，语义混乱。

**v2 重画**：画布加宽至 1120（键盘区 x<880、numpad 区独立 x≥900）；numpad 按真实结构重画（左半 10 键 Codex / 右半 7 键 WB，示例 Codex 溢 3 / WB 溢 2）；半区标签入格、图例下移通栏；新增 ⑤ 双模式卡片与"重新扫描"信号入口。生成后用 headless Edge 截图做渲染级验证（无重叠、无出界）。

### 11.5 台账补充

| 项 | 状态 |
| --- | --- |
| NOTICE 脉冲修复 + 5 用例 | **已完成，21/21 通过** |
| `rescan_sessions`（transcript 扫描）+ 12 用例 | **已完成，12/12 通过** |
| `toggle_partition` 双模式 + 托盘/设置窗/HTTP 三入口 | **已完成** |
| 图示 v2（修 3 处渲染错误 + 双模式/重扫描内容） | **已完成，渲染验证通过** |
| `tests/test_rescan.py`（12 用例） | **已完成** |
| 图示 v3（用户二次报障后重做，见 11.6） | **已完成，渲染验证通过** |

### 11.6 重做：亮灯逻辑图示 v3（用户二次报障"还是有排版错误，大点分区不拥挤"）

v2 推送后用户再次截图报障。截图定位 v2 遗留缺陷：

1. **整体拥挤**：画布 1120×1040，键格 16px、芯片 150×44，分区密；
2. **② 区任务标签是悬浮纯文字**，与所在排无视觉关联，键盘与 numpad 之间大片空白；
3. **numpad"WorkBuddy 溢出 · 7 键"标题超出画布被裁**：x=1024 起笔 + 文字宽 ≈96px > 1120 画布右缘，截图里显示为"WorkBuddy 溢出 -"缺尾（v2 引入的新出界）；
4. **③ 区"会话记录"盒子高 34px 装不下两行文字**（副标题基线 y+35 掉到盒底外），与紧随的通栏说明文字重叠；
5. **⑤ 区三卡片不等宽**（320/320/360）。

**v3 重做**（生成器 Temp/gen_lighting_svg.py，headless Edge 截图 + 分区裁剪放大核查）：

| 维度 | v2 | v3 |
| --- | --- | --- |
| 画布 | 1120×1040 | **1440×1520** |
| 主键格 | 16px | **22px**（溢出键格 34px） |
| 矩阵芯片 | 150×44 | **250×56** |
| 任务标签 | 悬浮文字 | **与每一排对齐的彩色 chip**（来源色圆点 + 文案） |
| numpad | 散排格子 | **独立白底面板**，两个半区标题各自居中（不再出界） |
| ③ 源盒子 | 34/48 高不一 | **统一 52 高，标题/副标题都入盒** |
| ⑤ 卡片 | 320/320/360 不等宽 | **三等宽 413** |
| ① 矩阵 | 3 列 | **4 列**（增补"通知 · 红闪 20s"，与待确认持续红闪区分——NOTICE 修复后图上必须体现两种红闪语义） |

验证：渲染截图 + numpad/③/键盘左区三处裁剪放大核查——半区标题完整、盒子文字入盒无重叠、排1 亮格 8/19 与任务 chip 对齐。

### 11.7 设置界面重构：分区/全局互斥置灰 + 效果同规格 + 亮度量表迁移（2026-10-14 下午，用户报障驱动）

用户截图报障设置窗五项问题：① 顶部"运行中"在分区模式下无响应（全局运行效果本就不生效，界面却可编辑）；② 分区模式缺少效果/速度/帧率/亮度设置；③ 按键摆放位置要优化；④ **分区与全局只能二选一**（选了分区，全局效果要暗下去不能选）；⑤ 分区和全局要有一样的颜色/帧率/速度选择，亮度默认设成 100。

**互斥置灰设计**（`tray.py` `_sync_mode_ui`）："分区模式"复选框常驻可点；勾选时 `f_run`（运行中，全局专属）递归置灰、`part_body`（分区设置子框）启用，反之亦然。递归置灰用 `_set_enabled`（try TclError 兼容无 state 选项的控件）。

**参数共用性分析**（哪些能置灰、哪些不能）：

| 参数 | 归属 | 可否置灰 |
| --- | --- | --- |
| `running.*`（mode/color/speed/fps/brightness） | 全局专属 | **可**（分区模式下不生效） |
| `waiting.color/brightness`、`done.times/on_ms/off_ms` | 两模式共用（分区红闪/绿闪直接读它们） | **不可** |
| `sources.*`、`partition.*` | 分区专属 | **可**（分区未启用时不生效） |

**分区效果补齐**：`partition.effect = flow|breathe`（面板 combobox，与全局运行中效果同规格），新增 `effects.row_breathe_map()` 呼吸包络（`0.5-0.5cos(2πt/period)`，亮格内同步渐明渐暗，lit_count 语义同流水）；分区速度/帧率/亮度滑条与全局同规格（整数量表）。

**亮度量表迁移**：0-1 浮点 → 0-100 整数百分比，默认 **100**。新增 `effects.bfactor()` 换算助手；`keylight._migrate_brightness()` 在 `load_config` 末尾把老配置的 `0<v<=1` 浮点 ×100 迁移（没有这段，老配置 0.8 会被当成 0.8% → 键盘几乎全黑）。设置窗待确认区补亮度滑条（原先硬编码 0.8），运行中亮度滑条 0.1-1.0 → 5-100。

**其他**：按钮区两行分组（`[保存并应用][确认并关闭][恢复硬件灯效]` / `[测试: 运行中][测试: 待确认][测试: 完成]`）；新增 `_ok()`（保存并直接关窗）；演示/重扫按钮统一 width=15；顶部新增模式提示标签（当前哪套生效）。

**测试**：`tests/test_partition.py` 新增 `TestBrightnessAndEffects`（12 用例）：bfactor 百分比换算与非法回落、row_breathe_map 包络/lit_count、compose_frame 的 breathe 分支（按 `row_phase` 反解确定性命中波前）、brightness=100 峰值键=来源色满值 / 50 半亮、`_migrate_brightness` 迁移语义、DEFAULTS 全为 100。**33/33 通过**（test_rescan 12/12、test_codex_tail 同步通过）。

踩坑记录：呼吸 1/4 周期处 `cos(π/2)` 浮点误差（6.1e-17）使 255×0.5 落到 127.4999…→round 127，断言需容差 ±1；`row_phase(now,speed,...) = xmin-tail + (now*speed)%span`，扫时间采样只能逼近波前，精确断言要反解 `now = ((x-(xmin-tail))%span)/speed`。
