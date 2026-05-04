# CLAUDE.md

本文件为 Claude Code（claude.ai/code）在此仓库中工作时提供指引。

## 项目概述

基于 NoneBot2 + OneBot v11 的 QQ 关键词转发机器人。监听指定 QQ 群消息，匹配关键词或正则，将命中的消息通过私聊转发给指定 QQ。支持定时提醒功能。

## 架构

```
bot.py                        # 入口 — 初始化 NoneBot，注册 OneBot v11 适配器，加载 apscheduler + plugins/
rules.py                      # 共享模块：rules.json 的加载、保存、查询、规范化（原子写入）
reminders.py                  # 共享模块：reminders.json 的加载、保存、规范化（原子写入）
plugins/
  __init__.py
  keyword_forward.py          # 核心插件：群消息监听、关键词匹配、去重、转发、管理命令、提醒命令与调度
manage_keywords.py            # CLI 工具：管理 rules.json
rules.json                    # 多群规则配置（热重载）
reminders.json                # 提醒配置（启动时恢复调度）
keywords.json                 # 旧版关键词文件（仅迁移时使用）
.env                          # 环境配置
```

### 数据流

```
QQ 群消息 → NapCatQQ（Windows）→ 反向 WebSocket → NoneBot2（服务器）
→ keyword_forward.py 匹配关键词 → 命中后通过 NapCatQQ 私聊转发

定时提醒 → APScheduler cron → _fire_reminder → NapCatQQ 私聊发送
```

### 关键设计细节

- **去重**：基于内存 deque + set，30 秒 TTL，跟踪 `(group_id, message_id)` 对
- **规则热重载**：每次群消息检查 `rules.json` 的 mtime，无需重启
- **提醒调度**：`nonebot-plugin-apscheduler` 管理 cron 任务，启动时从 `reminders.json` 恢复
- **原子写入**：`rules.py` 和 `reminders.py` 使用临时文件 + `os.replace()` 写入，防止文件损坏

## 命令

### 启动机器人
```bash
python bot.py
```

### 管理命令（通过 QQ 私聊发送）

关键词/规则命令（支持 `[群号]`，单群省略）：
- `status [群号]` — 查看规则
- `add [群号] <关键词>` — 添加关键词
- `remove [群号] <关键词>` — 删除关键词
- `set [群号] <词1,词2,...>` — 替换全部关键词
- `on [群号]` / `off [群号]` — 启用/禁用监听
- `help` — 显示帮助

提醒命令：
- `remind add <HH:MM> <内容>` — 添加每日提醒（如 `remind add 10:00 背单词`）
- `remind remove <编号>` — 删除提醒
- `remind list` — 查看所有提醒

高级命令（群/目标管理）：
- `rule addgroup <群号>` — 添加群规则
- `rule delgroup <群号>` — 删除群规则
- `rule addtarget <群号> <QQ>` — 添加转发目标
- `rule deltarget <群号> <QQ>` — 删除转发目标

### 安装依赖
```bash
pip install -r requirements.txt
```

## 配置（`.env`）

| 键 | 说明 |
|---|---|
| `HOST` / `PORT` | NoneBot 监听地址（默认 `0.0.0.0:8080`） |
| `ONEBOT_ACCESS_TOKEN` | OneBot 连接令牌 |
| `ALLOWED_GROUPS` | 要监听的群号，逗号分隔 |
| `TARGET_QQS` | 转发目标 QQ，逗号分隔 |
| `ADMIN_QQS` | 允许管理的关键词 QQ（留空默认等于 TARGET_QQS） |
| `KEYWORDS` | 初始关键词（仅旧版迁移时使用） |
| `RULES_FILE` | rules.json 路径（默认 `rules.json`） |
| `REMINDERS_FILE` | reminders.json 路径（默认 `reminders.json`） |
| `CASE_SENSITIVE` | 是否大小写敏感（默认 false） |
| `USE_REGEX` | 是否把关键词当正则匹配（默认 false） |

## `rules.json` 格式

```json
{
  "rules": [
    {
      "group_id": 554817535,
      "targets": [2731811629],
      "keywords": ["裤子"],
      "enabled": true,
      "use_regex": false
    }
  ]
}
```

## `reminders.json` 格式

```json
{
  "reminders": [
    {
      "id": 1,
      "hour": 10,
      "minute": 0,
      "message": "背单词",
      "targets": [2731811629],
      "enabled": true
    }
  ]
}
```