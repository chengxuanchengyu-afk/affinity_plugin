# QQ 好感度插件

按 QQ 号跨群维护好感度分值，并把用户划入可自定义的关系分组。同一个 QQ 号在不同群里共用一份分数。

- 插件 ID：`maibot-community.affinity-plugin`
- 数据文件：插件数据目录下的 `affinity.json`
- 所需能力：`send.text`（已在 `_manifest.json` 中声明）

## 快速开始

1. 把 `affinity_plugin` 目录放在宿主的插件目录下。
2. 编辑 `config.toml`，至少把自己的 QQ 号填进 `permission.admin_user_ids`，否则没人能使用管理命令。
3. 重启宿主。`_manifest.json` 里的能力声明可能被缓存，只重载插件不一定生效，建议完整重启。
4. 在群里发 `/好感度` 验证，机器人会把结果发回当前会话。

## 命令

| 命令 | 权限 | 说明 |
|---|---|---|
| `/好感度` · `/affinity` | 所有人 | 查询自己的好感度 |
| `/好感度 <QQ号>` · `/affinity <QQ号>` | 仅管理员 | 查询指定用户 |
| `/好感度设置 <QQ号> <分数>` · `/affinity_set` | 仅管理员 | 覆盖为指定分数 |
| `/好感度增加 <QQ号> <分数>` · `/affinity_add` | 仅管理员 | 在当前分数上加 |
| `/好感度减少 <QQ号> <分数>` · `/affinity_sub` | 仅管理员 | 在当前分数上减 |

分数支持小数，例如 `/好感度增加 123456 5.5`。结果一律截断在 0–100 之间，保留两位小数。

示例：

```
> /好感度
QQ 200002：好感度 20.00，关系：陌生人

> /好感度设置 123456 80
QQ 123456：好感度 20.00 → 80.00，关系：信任

> /好感度增加 123456 5.5
QQ 123456：好感度 80.00 → 85.50，关系：信任
```

非管理员查询他人会收到「只有管理员可以查询他人好感度。」，使用修改命令会收到「只有管理员可以修改好感度。」

命令末尾有严格锚定，`/好感度设置 123456 80 备注` 这种多余内容不会匹配任何命令。QQ 号必须是纯数字，长度 5–12 位。

## 谁算管理员

满足任一条件即可：

- QQ 号在 `permission.admin_user_ids` 白名单里
- `permission.allow_platform_admin = true`（默认）且该用户是当前群的群主或管理员

每次修改都会写一条审计记录到 `affinity.json` 的 `audit` 数组，包含操作者、目标、操作类型、改动前后分数，只保留最近 500 条。

## 配置说明

### `[plugin]`

| 键 | 默认 | 说明 |
|---|---|---|
| `enabled` | `true` | 是否启用消息采集。关掉后命令仍可用，但不再自动累积分数 |
| `initial_score` | `20.0` | 新用户初始分 |
| `config_version` | `"1.0.0"` | 配置版本标记 |
| `max_context_users` | `20` | 预留，见下方「尚未生效的配置」 |
| `max_prompt_length` | `2000` | 预留，同上 |

### `[batch]`

消息不会逐条计分，而是先缓冲，再定期批量结算。

| 键 | 默认 | 说明 |
|---|---|---|
| `update_interval_seconds` | `300` | 结算间隔秒数，代码里下限为 10 秒 |
| `max_users_per_request` | `50` | 单次提交给模型的最大用户数 |
| `max_messages_per_user` | `30` | 预留，见下方 |
| `max_pending_messages` | `1000` | 预留，见下方 |

### `[scoring]`

每次结算时，按本周期累积的行为次数乘以对应权重求和。

| 键 | 默认 | 触发条件 |
|---|---|---|
| `normal_message_delta` | `0.02` | 每条普通发言 |
| `mention_bot_delta` | `0.3` | @机器人 |
| `reply_bot_delta` | `0.4` | 回复机器人 |
| `continued_conversation_delta` | `0.2` | 延续对话 |
| `helpful_interaction_delta` | `0.5` | 帮助性互动 |
| `duplicate_message_delta` | `-0.2` | 重复消息 |
| `spam_delta` | `-0.3` | 刷屏 |
| `max_positive_delta_per_period` | `5.0` | 单次结算的涨幅上限 |
| `max_negative_delta_per_period` | `-5.0` | 单次结算的跌幅下限 |
| `llm_max_delta_per_period` | `5.0` | 模型评分的单次变化上限 |
| `max_positive_delta_per_day` | `10.0` | 预留，见下方 |
| `max_negative_delta_per_day` | `-10.0` | 预留，见下方 |

`mention_bot` 等行为标记依赖宿主在消息事件里提供对应字段（如 `mention_bot`、`is_reply_to_bot`、`is_spam`）。宿主没传的字段一律按 0 计，此时只有 `normal_message_delta` 会生效。

### `[llm]`

可选。开启后每次结算会把该周期的行为统计交给一个 OpenAI 兼容接口，让模型给出额外的分数调整，与本地权重算出的分值相加。

| 键 | 默认 | 说明 |
|---|---|---|
| `enabled` | `false` | 关闭时完全走本地权重 |
| `base_url` | `https://api.openai.com/v1` | 接口地址，会自动拼 `/chat/completions` |
| `api_key` | `""` | 密钥，敏感字段 |
| `model` | `gpt-4o-mini` | 模型名 |
| `timeout_seconds` | `30` | 请求超时 |
| `temperature` | `0.2` | 采样温度 |
| `max_tokens` | `2000` | 最大输出长度 |
| `send_message_content` | `false` | 是否把消息正文摘要发给模型 |

几点注意：

- 需要运行环境装了 `aiohttp`，否则会记一条警告并回退到本地评分。
- 请求失败、超时、返回不是合法 JSON，都会回退到本地评分，不会中断结算。
- `send_message_content = true` 会把用户发言内容（每条截断到 300 字，每人最多 30 条）发送到第三方接口。涉及隐私，默认关闭，开启前请确认合规。
- 模型返回的分数会被裁剪到 `llm_max_delta_per_period` 范围内，且只接受本次提交过的 QQ 号，防止模型凭空造数据。

### `[permission]`

| 键 | 默认 | 说明 |
|---|---|---|
| `admin_user_ids` | `[]` | 管理员白名单，填 QQ 号字符串 |
| `allow_platform_admin` | `true` | 是否把群主/群管理员也视为管理员 |
| `allow_admin_modify_other` | `true` | 关掉后管理员也只能改自己的分数 |
| `allow_admin_query_other` | `true` | 当前未被代码读取，查他人只看是否为管理员 |

### `[[groups]]`

分组决定分数对应的关系名称。可以自由增删条目，但有硬性校验，不满足会在插件加载时报错：

- `id` 和 `name` 不能为空，`id` 不能重复
- 每段范围满足 `0 <= min_score <= max_score <= 100`
- 所有分组必须**无缺口、无重叠地完整覆盖 0–100**，即第一段从 0 开始，最后一段到 100 结束，每段的 `min_score` 等于上一段 `max_score + 1`

默认四段：

```toml
[[groups]]
id = "stranger"
name = "陌生人"
min_score = 0
max_score = 25

[[groups]]
id = "acquaintance"
name = "认识"
min_score = 26
max_score = 50

[[groups]]
id = "friend"
name = "朋友"
min_score = 51
max_score = 75

[[groups]]
id = "trusted"
name = "信任"
min_score = 76
max_score = 100
```

改成两段也可以，只要覆盖完整：

```toml
[[groups]]
id = "normal"
name = "普通"
min_score = 0
max_score = 59

[[groups]]
id = "close"
name = "亲近"
min_score = 60
max_score = 100
```

## 身份识别

插件从消息里多个位置提取 QQ 号（`user_id`、`sender.user_id`、`user_info.user_id` 等），要求所有能提取到的值**互相一致**，否则放弃本条消息。这是为了避免转发、伪造场景下把分数记到错误的人头上。所以少数消息不计分是预期行为。

消息 ID 会去重，同一条消息重复投递不会重复计分。

## 尚未生效的配置

以下配置项当前在代码里没有被读取，填了不会有效果。保留是为了后续扩展，这里如实标注避免误解：

| 配置 | 情况 |
|---|---|
| `[decay]` 整节 | `scoring.py` 里有 `decay_delta()` 实现，但 `plugin.py` 没有调用，长期不互动不会自动掉分 |
| `scoring.max_positive_delta_per_day` / `max_negative_delta_per_day` | `apply_daily_limit()` 已实现但未接入，每日累计上限不生效 |
| `plugin.max_context_users` / `max_prompt_length` | `prompt_builder.py` 提供了向 Planner 注入关系元数据的能力，但插件未注册对应的 Hook，注入未启用 |
| `batch.max_messages_per_user` / `max_pending_messages` | 未接入。实际每人摘要条数由 `storage.py` 硬编码为 30 条，缓冲总量无上限 |
| `permission.allow_admin_query_other` | 未被读取 |

## 数据文件

`affinity.json` 结构：

```json
{
  "version": 1,
  "users": {
    "123456": {
      "qq_id": "123456",
      "score": 85.5,
      "total_messages": 42,
      "last_seen_at": "2026-07-28T10:00:00+00:00"
    }
  },
  "audit": [
    {
      "operator_qq_id": "100001",
      "target_qq_id": "123456",
      "operation": "set",
      "old_score": 20.0,
      "new_score": 80.0
    }
  ]
}
```

写入采用临时文件加原子替换，中途断电不会留下半截文件。备份直接复制这个文件即可；手工编辑请先停掉宿主，否则会被内存中的状态覆盖。

## 排查

**命令没反应**

先看日志有没有 `命令执行成功: affinity`。有但群里没消息，通常是 `send.text` 能力没生效，确认 `_manifest.json` 的 `capabilities` 含 `"send.text"` 并完整重启宿主。日志里若出现「好感度命令缺少 stream_id，无法发送回复」，说明宿主没传会话 ID，需要看适配器。

**管理命令提示无权限**

确认 `admin_user_ids` 里填的是字符串形式的 QQ 号，且与消息中识别到的号码一致。

**分数一直不涨**

检查 `plugin.enabled` 是否为 `true`，并确认已经过了一个 `update_interval_seconds` 周期。默认 300 秒且普通发言只有 0.02 分，涨得很慢是正常的。想快速看到效果可以把间隔调小、权重调大。

**加载时报「好感度分组必须完整覆盖 0-100」**

分组存在缺口或重叠，按上面「`[[groups]]`」一节的规则重新对齐边界。