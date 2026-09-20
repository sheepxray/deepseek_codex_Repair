# deepseek_codex_Repair — DeepSeek Responses 请求修复代理

一个本地反向代理：拦截 OpenAI Responses API（`POST /v1/responses`）请求，在转发给 DeepSeek 上游**之前**自动修复 `input[]` 数组中损坏的 `function_call` / `function_call_output` item，上游响应（含 SSE 流式）原样透传。

**只修复、不翻译**：JSON schema 形状完全保持，仅增/改/删 `input[]` 内的 item，其余字段（`model`、`tools`、`instructions`、`previous_response_id` 等）原样保留。上游需接受 Responses 格式（官方 API 或第三方网关均可）。

## 解决什么问题

Responses API 客户端（如 Codex 等）的请求历史中经常出现三类损坏的 function-call 数据，会被上游 400 拒绝：

| 问题 | 示例 | 后果 |
|---|---|---|
| 孤立的 `function_call_output` | 输出的 `call_id` 在 input 中找不到对应 `function_call` | 上游拒绝："unmatched call_id" |
| 缺少 `call_id` | 输出没有 `call_id` 字段（或为 null / 非字符串） | 无法配对，上游校验失败 |
| 历史脏数据 | 上下文压缩/截断后残留的孤儿 item | 整个请求被拒 |

代理在转发前自动修复这些问题，客户端无需改动。

```
[客户端] --POST /v1/responses（input[] 可能损坏）--> [代理 127.0.0.1:8080] --> [UPSTREAM_URL]
    ^                                                     | 修复 input[]             |
    +---------- SSE 字节流 / JSON 原样 + X-Proxy-Repairs 头 +-------------------------+
```

## 快速开始

1. 双击 `run_proxy.bat`
   - 首次运行自动创建 `.venv` 并安装依赖（清华 PyPI 镜像优先，失败回退官方源）
   - 依赖检查按"实际可导入"判断，装一半失败下次运行会自动重试
2. 把客户端的 `base_url` 指向代理：

```
http://127.0.0.1:8080/v1    （或 http://127.0.0.1:8080，路径原样中继）
```

3. 保留客户端原有的 DeepSeek API key（代理原样转发）；如需代理统一注入 key，设置环境变量 `PROXY_API_KEY`

手动运行（等价方式）：

```bash
pip install -r requirements.txt
python -m uvicorn app.main:app --host 127.0.0.1 --port 8080
```

## 修复规则

### 1. 孤立输出 → 转消息（`ORPHAN_STRATEGY=convert_to_user`，默认）

```jsonc
// 修复前
[
  {"type": "function_call", "name": "get_weather", "arguments": "{}", "call_id": "call_1"},
  {"type": "function_call_output", "call_id": "call_dead", "output": "sunny"}
]
// 修复后（孤立输出转 user 消息）
[
  {"type": "function_call", "name": "get_weather", "arguments": "{}", "call_id": "call_1"},
  {"type": "message", "role": "user",
   "content": [{"type": "output_text", "text": "sunny"}]}
]
```

可选策略：`convert_to_developer`（转 developer 消息）、`remove`（直接移除）。

### 2. 缺 call_id → 位置配对 / 合成注入（`MISSING_ID_STRATEGY=synthesize`，默认）

```jsonc
// 修复前（输出缺 call_id，前导 function_call 未消费）
[
  {"type": "function_call", "name": "get_weather", "arguments": "{}", "call_id": "call_1"},
  {"type": "function_call_output", "output": "sunny"}          // 无 call_id
]
// 修复后（采用前导 function_call 的 call_id）
[
  {"type": "function_call", "name": "get_weather", "arguments": "{}", "call_id": "call_1"},
  {"type": "function_call_output", "call_id": "call_1", "output": "sunny"}
]
```

若没有可配对的前导 `function_call`（输出在 index 0 / 前导已被消费），则生成新 call_id 并注入合成 `function_call`：

```jsonc
// 修复前
[{"type": "function_call_output", "output": "sunny"}]
// 修复后
[
  {"type": "function_call", "name": "proxy_synthetic", "arguments": "{}", "call_id": "call_proxy_0"},
  {"type": "function_call_output", "call_id": "call_proxy_0", "output": "sunny"}
]
```

可选策略：`convert_to_user`（不合成配对，直接转 user 消息）。

### 3. 全部规则一览

| # | 规则名 | 触发条件 | 修复动作 | 控制项 |
|---|--------|---------|---------|--------|
| 1 | `remove_non_dict_item` | input 中混入非 dict item | 移除（不可配置） | — |
| 2 | `synthesize_function_call_id` | `function_call` 缺/空/非字符串 `call_id` | 生成 `call_proxy_N`（冲突自动递增） | `SYNTHETIC_CALL_PREFIX` |
| 3 | `adopt_call_id_from_preceding_function_call` | 输出缺 `call_id` 且前导 fc 未被消费 | 采用前导 fc 的 `call_id`（位置配对） | `MISSING_ID_STRATEGY` |
| 4 | `inject_synthetic_function_call` | 输出缺 `call_id` 且无可采用的前导 fc | 生成新 id 并在输出前注入合成 `function_call` | `MISSING_ID_STRATEGY` |
| 5 | `convert_missing_id_output_to_user` | 同上 + `MISSING_ID_STRATEGY=convert_to_user` | 转 user 消息 | `MISSING_ID_STRATEGY` |
| 6 | `convert_orphan_to_user` / `convert_orphan_to_developer` | 输出有 `call_id` 但无匹配 fc（孤立） | 转 user/developer 消息 | `ORPHAN_STRATEGY` |
| 7 | `remove_orphan_output` | 同上 + `ORPHAN_STRATEGY=remove` | 移除 | `ORPHAN_STRATEGY` |
| 8 | `remove_unfixable_output` | 输出无 `output` 内容，无法转消息 | 无论策略一律移除 | — |

细节保证：

- 连续两个缺 call_id 的输出**不会共享**同一个配对：第一个采用前导 fc，第二个拿独立合成 pair
- 已被输出消费过的 fc **不会**被重复采用
- 原请求对象绝不修改（内部 deepcopy）；坏输入不崩溃

刻意**不修复**的（保守设计，仅 debug 日志）：末尾悬空 `function_call`、重复的 output `call_id`、未知 item 类型（`reasoning`/`message`/自定义）一律原样透传。

## 配置（环境变量）

| 变量 | 默认 | 说明 |
|---|---|---|
| `PROXY_HOST` | `127.0.0.1` | 监听地址 |
| `PROXY_PORT` | `8080` | 监听端口 |
| `UPSTREAM_URL` | `https://api.deepseek.com` | 上游基址，路径/查询串原样拼接 |
| `PROXY_API_KEY` | 未设 | 设置则覆盖客户端 Authorization |
| `ORPHAN_STRATEGY` | `convert_to_user` | 孤立输出策略：`convert_to_user` / `convert_to_developer` / `remove` |
| `MISSING_ID_STRATEGY` | `synthesize` | 缺 call_id 策略：`synthesize`（补 id/注入配对）/ `convert_to_user` |
| `SYNTHETIC_CALL_PREFIX` | `call_proxy_` | 合成 call_id 前缀 |
| `LOG_LEVEL` | `INFO` | `DEBUG` 显示逐趟修复细节 |
| `DEBUG_DUMP` | `0` | `1`/`true`/`yes`/`on` → 修复前后请求落盘 `%TEMP%\deepseek_codex_repair_dump_*.json` |
| `MAX_BODY_BYTES` | `52428800` | 请求体上限（超限 413） |
| `TIMEOUT_CONNECT` / `TIMEOUT_READ` | `10` / `600` | 上游连接/读取超时（秒，read 需容纳长流式） |

示例（Windows 命令行）：

```bat
set ORPHAN_STRATEGY=remove
set DEBUG_DUMP=1
run_proxy.bat
```

## 可观测性

- 每次修复打一条日志：`repair rule=<规则名> index=<input 原始索引> detail=<详情>`
- 响应头 `X-Proxy-Repairs: <N>` 表示本次请求修复了几处（0 处不加）
- `DEBUG_DUMP=1` 时修复前后完整请求对比落盘 `%TEMP%`

## 端点

| 端点 | 行为 |
|---|---|
| `POST /v1/responses` | 修复 input[] 后转发；`stream=true` 走 SSE 原始字节流式中继 |
| 其余任意路径/方法 | 透明反向代理（`GET /v1/models` 等） |
| `GET /healthz` | 健康检查，不访问上游 |

## 测试

```bash
pip install pytest pytest-asyncio
python -m pytest -q        # 53 个用例：修复规则单测 + 配置校验 + 集成 E2E
```

curl 冒烟（用真实 key 走真实 DeepSeek）：

```bash
curl http://127.0.0.1:8080/healthz
# 构造损坏请求：孤立输出 call_dead
curl -i -X POST http://127.0.0.1:8080/v1/responses -H "Authorization: Bearer sk-<key>" -H "Content-Type: application/json" ^
  -d "{\"model\":\"deepseek-chat\",\"input\":[{\"type\":\"function_call\",\"name\":\"get_weather\",\"arguments\":\"{}\",\"call_id\":\"call_1\"},{\"type\":\"function_call_output\",\"call_id\":\"call_dead\",\"output\":\"sunny\"}],\"stream\":false}"
```

## 排查指南

| 症状 | 原因 | 解决 |
|---|---|---|
| `No module named uvicorn` | 首次安装失败（PyPI 直连超时） | 重新双击 `run_proxy.bat`，脚本会自动重试（镜像优先）；或手动 `pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt` |
| 客户端 `connection refused` | 代理未启动 | 先启动代理，`curl http://127.0.0.1:8080/healthz` 验证 |
| 上游 401 | API key 未转发 | 确认客户端带了 `Authorization: Bearer <key>`；或设置 `PROXY_API_KEY` 统一注入 |
| 仍有 400 报错 | 存在未覆盖的坏数据 | `DEBUG_DUMP=1` 后把 `%TEMP%` 里的修复前后请求对比发出来分析 |
| `[Errno 10048]` 端口被占用 | 8080 已被占用 | `set PROXY_PORT=其他端口` 后重启 |

## 局限性

- JSON 会重新序列化（空白变化、语义不变）
- 单上游、无重试；上游不可达返回 502，超时返回 504
- 末尾悬空 `function_call` 有意保留不动（防止破坏被截断的多轮对话）

## 项目结构

```
app/
  repair.py      # 修复核心（纯函数，4 趟修复，零 HTTP 依赖）
  config.py      # 环境变量配置加载与校验
  proxy.py       # 上游转发：SSE 流式中继 / 缓冲响应 / 502/504 错误映射
  debug_dump.py  # DEBUG_DUMP 调试落盘
  main.py        # FastAPI 路由与编排
tests/           # 53 个测试用例
run_proxy.bat    # Windows 一键启动
```

## 参考

设计借鉴了 [dsv4-cc-proxy](https://github.com/HosheaLi/dsv4-cc-proxy)（纯函数 + deepcopy + 多趟后处理风格）、[deepseek-bridge](https://pypi.org/project/deepseek-bridge/)、[ccswitch-deepseek](https://github.com/liuzhengming/ccswitch-deepseek)。
