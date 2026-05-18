# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

基于 NoneBot2 + OneBot v11 的 QQ 关键词转发机器人。监听指定 QQ 群消息，匹配关键词或正则，将命中的消息通过私聊转发给指定 QQ。支持定时提醒功能。

## 架构

```
bot.py                        # 入口 — 初始化 NoneBot，注册 OneBot v11 适配器，加载 apscheduler + plugins/
rules.py                      # 共享模块：rules.json 的加载、保存、查询、规范化（原子写入）
reminders.py                  # 共享模块：reminders.json 的加载、保存、规范化（原子写入）
stats.py                      # 共享模块：stats.json 的加载、保存（原子写入，统计持久化）
session_manager.py            # 会话管理：用户交互状态跟踪，5分钟超时自动清理
image_renderer.py             # 图片渲染：Pillow 生成样式化 PNG 卡片（QQ 私聊不支持 markdown）
quotes.py                     # 名言获取：hitokoto API + 本地 fallback
start.sh                      # Linux 启动脚本（激活 venv + 运行 bot.py）
plugins/
  __init__.py
  keyword_forward.py          # 核心插件：群消息监听、关键词匹配、去重、转发、管理命令、交互菜单
  remind.py                   # 提醒插件：定时提醒的调度、触发、管理命令
manage_keywords.py            # CLI 工具：管理 rules.json
rules.json                    # 多群规则配置（热重载）
reminders.json                # 提醒配置（首次添加提醒时自动创建，启动时恢复调度）
keywords.json                 # 旧版关键词文件（仅迁移时使用）
.env                          # 环境配置
```

> **注意**：项目根目录曾有旧版 `keyword_forward.py`，当前插件位于 `plugins/keyword_forward.py`。

### 推荐部署架构

- **服务器**（Ubuntu）：运行 NoneBot2 Python 服务
- **本机**（Windows）：运行 QQ + NapCatQQ
- NapCatQQ 通过反向 WebSocket 主动连接服务器

### 数据流

```
QQ 群消息 → NapCatQQ（Windows）→ 反向 WebSocket → NoneBot2（服务器）
→ keyword_forward.py 匹配关键词 → 命中后通过 NapCatQQ 私聊转发

定时提醒 → APScheduler cron → remind.py _fire → NapCatQQ 私聊发送给创建者
```

### 关键设计细节

- **去重**：基于内存 deque + set，30 秒 TTL，跟踪 `(group_id, message_id)` 对
- **规则热重载**：每次群消息检查 `rules.json` 的 mtime，无需重启
- **提醒调度**：`plugins/remind.py` 管理 cron 任务（`nonebot-plugin-apscheduler`），启动时自动从 `reminders.json` 恢复，提醒发送给创建者而非全局 TARGET_QQS
- **原子写入**：`rules.py` 和 `reminders.py` 使用临时文件 + `os.replace()` 写入，防止文件损坏
- **图片消息**：QQ 私聊不支持 markdown 渲染，使用 `image_renderer.py` 生成样式化 PNG 卡片（Pillow），支持两列布局、section headers、emoji
- **关键词冷却**：每个关键词在同一群内 15 秒冷却，防止短时间重复触发
- **统计追踪**：按日期+群+关键词统计命中次数，持久化到 `stats.json`（每 60 秒刷新，关闭时保存）
- **命中标注**：转发消息自动追加 "命中: 关键词" 标注，接收者可识别触发原因
- **关键词独立目标**：每个关键词可设置独立的转发目标，未设置时回退到规则级目标
- **交互菜单**：`session_manager.py` 管理用户会话状态，5分钟超时，支持数字选项菜单交互（模拟按钮体验）

## 命令

### 环境要求
- Python 3.10+

### 开发环境
```bash
python -m venv .venv
.venv\Scripts\activate  # Windows
pip install -r requirements.txt
```

### 启动机器人
```bash
python bot.py
```

### 管理命令（通过 QQ 私聊发送）

关键词/规则命令（支持 `[群号]`，单群省略）：
- `status [群号]` — 查看规则
- `add [群号] <关键词>` — 添加关键词
- `remove [群号] <编号>` — 按编号删除关键词（用 `status` 查看编号）
- `set [群号] <词1,词2,...>` — 替换全部关键词
- `disable [群号] <编号>` — 临时禁用关键词
- `enable [群号] <编号>` — 恢复禁用关键词
- `on [群号]` / `off [群号]` — 启用/禁用监听
- `stats` — 今日关键词命中统计
- `quote [HH:MM]` — 设置每日一言定时提醒（默认 09:00）
- `help` — 显示帮助

提醒命令：
- `remind <HH:MM> <内容>` — 添加每日提醒（快捷方式，如 `remind 10:00 背单词`）
- `remind add <HH:MM> <内容>` — 添加每日提醒（完整语法）
- `remind once <YYYY-MM-DD> <HH:MM> <内容>` — 单次提醒
- `remind workday <HH:MM> <内容>` — 工作日提醒（周一至周五）
- `remind interval <分钟> <内容>` — 间隔提醒
- `remind period <HH:MM> <分钟> <内容>` — 周期催促（不完成一直催）
- `remind quote <HH:MM>` — 每日一言（随机名言，自动生成）
- `remind edit <编号> <字段> <值>` — 编辑提醒（字段: time / message / interval）
- `remind done <编号>` — 标记周期催促今日完成
- `remind remove <编号>` — 删除提醒
- `remind list` — 查看所有提醒

高级命令（群/目标管理）：
- `rule addgroup <群号>` — 添加群规则
- `rule delgroup <群号>` — 删除群规则
- `rule addtarget <群号> <QQ>` — 添加转发目标
- `rule deltarget <群号> <QQ>` — 删除转发目标
- `kwtarget add <群号> <关键词编号> <QQ>` — 添加关键词独立目标
- `kwtarget del <群号> <关键词编号> <QQ>` — 删除关键词独立目标

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
| `STATS_FILE` | stats.json 路径（默认 `stats.json`） |
| `CASE_SENSITIVE` | 是否大小写敏感（默认 false） |
| `USE_REGEX` | 是否把关键词当正则匹配（默认 false） |

## `rules.json` 格式

```json
{
  "rules": [
    {
      "group_id": 554817535,
      "targets": [2731811629],
      "keywords": [
        {"word": "裤子", "enabled": true},
        {"word": "鞋子", "enabled": true, "targets": [123456]}
      ],
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
      "type": "daily",
      "hour": 10,
      "minute": 0,
      "message": "背单词",
      "targets": [],
      "enabled": true,
      "creator_qq": 2731811629
    },
    {
      "id": 2,
      "type": "period",
      "hour": 18,
      "minute": 0,
      "repeat_interval": 10,
      "message": "背单词",
      "last_done_date": "2026-05-05",
      "targets": [],
      "enabled": true,
      "creator_qq": 2731811629
    },
    {
      "id": 3,
      "type": "daily",
      "hour": 9,
      "minute": 0,
      "auto_generate": "quote",
      "targets": [],
      "enabled": true,
      "creator_qq": 2731811629
    }
  ]
}
```

提醒类型：
- `daily` — 每天固定时间
- `workday` — 工作日（周一至周五）
- `once` — 单次提醒（需要 `date` 字段，触发后自动删除）
- `interval` — 间隔提醒（需要 `interval_minutes` 字段）
- `period` — 周期催促（每天固定时间开始，每隔 N 分钟催促一次，直到标记完成）

特殊字段：
- `auto_generate: "quote"` — 触发时自动生成随机名言，不使用 `message` 字段
- `last_done_date` — 周期催促类型专用，记录最后完成日期

## 图片渲染 (`image_renderer.py`)

QQ 私聊不支持 markdown 渲染，所有格式化消息通过 Pillow 生成 PNG 图片卡片。

设计常量：
- `CARD_WIDTH = 920` — 卡片宽度
- `PADDING = 42` — 边距
- `COL2_X = 490` — 两列布局时第二列起始位置（左对齐）
- `CORNER_RADIUS = 18` — 圆角半径
- `FONT_SIZE = 19` — 正文字号
- `TITLE_FONT_SIZE = 24` — 标题字号

两列布局检测：
- 正则 `r"^(\S.+?)  {3,}(\S.+)$"` 匹配 3+ 空格分隔的行
- 检测条件：`PADDING + 命令宽度 + 56px <= COL2_X`
- 命令在左（x=42），描述在右（x=490），左对齐
- 不满足条件时降级为单行正文自动换行

字体优先级（跨平台中文支持）：
1. Linux: wqy-microhei, wqy-zenhei, NotoSansCJK
2. macOS: PingFang, STHeiti
3. Windows: msyh (微软雅黑), simhei, simsun

## 开发注意事项

### Git 配置
项目应包含 `.gitignore`，排除：
- `__pycache__/` — Python 字节码缓存
- `.venv/` — 虚拟环境
- `.env` — 环境配置（含敏感信息）
- `*.pyc` — 编译的 Python 文件

### 交互菜单系统
- `status` 命令显示规则时会附带数字选项菜单（1-5）
- 用户回复数字触发对应操作，无需输入完整命令
- 会话状态存储在 `_session_manager`，5分钟自动过期
- 每10分钟自动清理过期会话（通过 APScheduler）
- 会话状态类型：
  - `menu_status` — 显示菜单后等待用户选择
  - `awaiting_keyword_add` — 等待用户输入新关键词
  - `awaiting_keyword_remove` — 等待用户输入要删除的编号
  - `awaiting_keyword_toggle` — 等待用户输入要切换状态的编号

### 修改命令交互
- 所有格式化输出使用 `_reply_image(bot, user_id, text, title)` 生成图片消息
- 简短单行消息（如确认、错误）可用 `_reply_private(bot, user_id, text)` 纯文本
- 帮助文本使用两列布局：命令与描述之间至少 3 个空格

### 修改提醒功能
- 新增提醒类型需在 `REMIND_TYPE_LABELS` 添加标签
- 调度逻辑在 `_schedule()` 中根据 `type` 字段分发
- 触发逻辑在 `_fire()` 中处理，注意 `auto_generate` 特殊字段
- 周期催促需同时管理 cron 任务和 interval 任务

### 修改图片渲染
- 修改样式常量后需重启机器人
- 字体缓存在 `_font_cache` 中，按字号缓存
- 两列布局检测阈值 `COL2_X - PADDING - 56 = 392px`，超过此宽度的命令会降级为单行
- Section headers 以 `—`/`─`/`━` 开头，渲染为蓝色带左侧竖条
- Separator lines 全为 `─━—－-` 字符，渲染为水平分隔线