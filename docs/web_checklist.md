# Web 改造 Checklist

## Architecture
- Web 后端通过 `ok/web.py` 提供静态资源 + Runtime API（HTTP JSON）+ WebSocket Runtime Stream，前端与 Runtime 控制通过 API 解耦。
- Runtime 控制复用 `OK` / `StartController` / `TaskExecutor` 现有核心能力，不复用 Qt UI 组件。
- Runtime/log 推送由 `RuntimeEventStream` 管理客户端与广播，日志通过 logger handler 桥接到 websocket。

## API
- `GET /api/runtime/status`：获取 runtime 初始化状态、执行器状态、当前任务、队列、任务列表状态。
- `POST /api/runtime/start`：启动指定 one-time task（`task` 可选，`exit_after` 可选）。
- `POST /api/runtime/stop`：停止指定任务或全量停止（禁用队列/触发任务并暂停执行器）。
- `GET /api/config/get`：获取 runtime/browser/device 当前配置快照。
- `POST /api/config/update`：更新 runtime/browser/device 配置补丁并返回最新配置。
- `GET /ws/runtime`：建立 websocket 连接并接收 `hello`/`runtime_status`/`task_event`/`config_event`/`log`/`error` 事件。

## Runtime Flow
1. `OK.startup_deploy_frontend()` 注入 `FrontendRuntimeAPI` 回调。
2. `FrontendRequestHandler` 接收 `/api/runtime/*` 请求并调用 `OK` Runtime 回调，同时发布 `task_event`。
3. `RuntimeEventStream` 通过 `/ws/runtime` 广播状态与日志：周期推送 `runtime_status`，日志推送 `log`。
4. `OK.start_runtime_task()` 复用 `StartController.do_start()` 触发运行；`OK.stop_runtime_task()` 负责停止/暂停。
5. 配置通过 `OK.get_runtime_config()/update_runtime_config()` 统一暴露与应用，并可触发 stream `config_event`。

---

## Module: task start/stop runtime integration

### Goal
- [x] DONE：提供可独立调用的 runtime start/stop/status 接口，作为 Web 前后端解耦基础。

### Current Status
- [x] DONE：已接入 HTTP Runtime API 并绑定到 `OK` runtime 控制方法。
- 实现说明：新增 `FrontendRuntimeAPI` 与 `/api/runtime/*` 路由，复用现有任务启动/停止逻辑。
- 相关文件：`/home/runner/work/ok-script/ok-script/ok/web.py`，`/home/runner/work/ok-script/ok-script/ok/__init__.py`

### API
- [x] DONE：`GET /api/runtime/status`
- [x] DONE：`POST /api/runtime/start`
- [x] DONE：`POST /api/runtime/stop`

### Frontend
- [ ] TODO：接入 runtime API 调用与错误提示展示。
- 优先级：P1
- 依赖：websocket runtime/log stream（P1）可并行，runtime status panel（P1）依赖本 API。

### Backend
- [x] DONE：运行时回调注入、任务启停与状态快照。
- [ ] TODO：完善 trigger task 启动策略（当前 start API 仅支持 one-time task）。
- 优先级：P2
- 依赖：无

### Tests
- [x] DONE：新增 runtime API 单测（status/start/stop + 非法 JSON）。
- 相关文件：`/home/runner/work/ok-script/ok-script/tests/test_web_runtime_api.py`

### Risks
- [ ] TODO：当前为 HTTP 轮询，日志与状态实时性不足。
- 优先级：P1
- 依赖：websocket runtime/log stream

### Next Step
- [~] IN PROGRESS：推进 websocket runtime/log stream，实现状态与日志推送通道。

---

## Module: websocket runtime/log stream

### Goal
- [x] DONE：建立 websocket-first 的 runtime 状态与日志流最小闭环。

### Current Status
- [x] DONE：已实现 `/ws/runtime`、事件广播与日志桥接。
- 实现说明：新增 `RuntimeEventStream`、`WebSocketLogHandler`、`RuntimeWebSocketClient`，支持 `hello/runtime_status/task_event/log/error` 事件。
- 相关文件：`/home/runner/work/ok-script/ok-script/ok/web.py`

### API
- [x] DONE：已定义并落地 websocket 事件协议（`hello` / `runtime_status` / `task_event` / `log` / `error`）。

### Frontend
- [ ] TODO：建立 websocket 客户端连接与重连机制。

### Backend
- [x] DONE：runtime 状态（周期推送）与日志（logger handler）已桥接 websocket 广播层。
- [ ] TODO：补充客户端背压与慢连接淘汰策略。
- 优先级：P1
- 依赖：无

### Tests
- [x] DONE：新增 websocket 握手与事件推送测试（task_event/log）。
- 相关文件：`/home/runner/work/ok-script/ok-script/tests/test_web_runtime_api.py`
- [ ] TODO：补充断线重连与高并发广播测试。
- 优先级：P1
- 依赖：无

### Risks
- [ ] TODO：线程与事件并发安全、背压处理（当前慢连接仅在发送失败时清理）。
- 优先级：P1
- 依赖：runtime status panel（P1）实时刷新能力受其影响。

### Next Step
- [~] IN PROGRESS：推进 runtime status panel 对接 websocket 事件与重连机制。

---

## Module: config api integration

### Goal
- [x] DONE：提供配置读取/更新 API。

### Current Status
- [x] DONE：已实现 `/api/config/get` 与 `/api/config/update` 最小闭环。
- 实现说明：`FrontendRuntimeAPI` 增加 config 回调，`OK` 新增 `get_runtime_config/update_runtime_config`，支持 runtime/browser/device 配置补丁。
- 相关文件：`/home/runner/work/ok-script/ok-script/ok/web.py`，`/home/runner/work/ok-script/ok-script/ok/__init__.py`

### API
- [x] DONE：`GET /api/config/get`
- [x] DONE：`POST /api/config/update`

### Frontend
- [ ] TODO：配置面板对接 API。

### Backend
- [x] DONE：已补充 `runtime/device/browser` 字段级白名单与类型校验（在 web API 层拦截非法 patch）。
- [ ] TODO：补充更细粒度错误码映射（当前以 message 文本区分错误类型）。
- 优先级：P2
- 依赖：无

### Tests
- [x] DONE：新增 config get/update API 测试。
- 相关文件：`/home/runner/work/ok-script/ok-script/tests/test_web_runtime_api.py`
- [x] DONE：新增非法 section/非法字段/非法类型测试（返回 400）。
- [ ] TODO：补充设备不可用场景测试（`device_manager` 未初始化时 update 失败路径）。
- 优先级：P1
- 依赖：无

### Risks
- [ ] TODO：配置热更新时与任务执行状态冲突（特别是运行中切换 capture/interaction）。
- 优先级：P1
- 依赖：runtime status panel 需要展示配置变更失败原因。

### Next Step
- [~] IN PROGRESS：补充配置错误码规范与设备不可用场景测试。

---

## Module: runtime status panel

### Goal
- [ ] TODO：前端展示 runtime 状态、当前任务、队列、错误信息。

### Current Status
- [ ] TODO：未实现。

### API
- [~] IN PROGRESS：已具备 `/api/runtime/status`，待 websocket 增量状态。

### Frontend
- [ ] TODO：状态卡片与任务队列视图。

### Backend
- [~] IN PROGRESS：状态快照已提供，待实时推送。

### Tests
- [ ] TODO：面板状态映射测试。

### Risks
- [ ] TODO：轮询频率过高导致额外负载。

### Next Step
- [ ] TODO：基于 websocket 事件驱动刷新面板。

---

## Module: device management api

### Goal
- [ ] TODO：暴露设备列表、设备切换、捕获方式切换 API。

### Current Status
- [ ] TODO：未实现。

### API
- [ ] TODO：定义 `/api/devices/list`、`/api/devices/select`、`/api/devices/capture`。

### Frontend
- [ ] TODO：设备管理页与状态展示。

### Backend
- [ ] TODO：复用 `DeviceManager` 的 refresh/select/capture 接口。

### Tests
- [ ] TODO：设备枚举与切换行为测试。

### Risks
- [ ] TODO：不同平台设备能力差异导致接口行为不一致。

### Next Step
- [ ] TODO：先固化统一设备数据模型与错误码。
