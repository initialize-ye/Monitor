# GitHub Actions Workflows

本项目包含三个自动化工作流，用于部署、健康检查和回滚。

## 📦 Deploy（自动部署）

**触发条件**：
- 推送到 `main` 分支时自动触发
- 手动触发：Actions → Deploy → Run workflow

**功能**：
1. ✅ 备份当前版本
2. ✅ 拉取最新代码
3. ✅ 检查代码变更
4. ✅ 安装中文字体（首次）
5. ✅ 更新依赖
6. ✅ Python 语法检查
7. ✅ 检查配置文件
8. ✅ 重启服务
9. ✅ 健康检查（服务状态 + 错误日志）
10. ✅ 自动回滚（失败时）

**特性**：
- 🎨 彩色输出，易于阅读
- 🔄 自动回滚机制
- 📊 显示代码变更
- ⚡ 跳过无变化的部署
- 🛡️ 语法检查防止错误代码上线

## 🏥 Health Check（健康检查）

**触发条件**：
- 每 6 小时自动运行
- 手动触发：Actions → Health Check → Run workflow

**检查项**：
- ✅ 服务运行状态
- ✅ 服务运行时长
- ✅ 内存使用情况
- ✅ 最近 6 小时错误数
- ✅ 磁盘空间使用

**告警阈值**：
- 错误数 > 10 次/6小时
- 磁盘使用 > 80%

## ⏮️ Rollback（快速回滚）

**触发条件**：
- 仅手动触发：Actions → Rollback → Run workflow

**使用方法**：

### 回滚到上一次部署
1. 进入 Actions → Rollback
2. 点击 "Run workflow"
3. 留空 commit 输入框
4. 点击 "Run workflow"

### 回滚到指定提交
1. 进入 Actions → Rollback
2. 点击 "Run workflow"
3. 输入提交 SHA（如 `67c7455`）
4. 点击 "Run workflow"

**功能**：
- 🔄 回滚代码到指定版本
- 🔄 自动重启服务
- 📊 显示回滚变更
- ✅ 验证回滚后服务状态

## 🔐 配置 Secrets

在 GitHub 仓库设置中添加以下 Secrets：

| Secret | 说明 | 示例 |
|--------|------|------|
| `SSH_HOST` | 服务器 IP 地址 | `123.45.67.89` |
| `SSH_USER` | SSH 用户名 | `ubuntu` |
| `SSH_KEY` | SSH 私钥 | `-----BEGIN OPENSSH PRIVATE KEY-----...` |

**设置路径**：
Settings → Secrets and variables → Actions → New repository secret

## 📝 查看日志

### 查看 Workflow 日志
1. 进入 Actions 标签
2. 选择对应的 workflow run
3. 点击查看详细日志

### 查看服务器日志
```bash
# 实时日志
sudo journalctl -u qq-forward-bot -f

# 最近 50 行
sudo journalctl -u qq-forward-bot -n 50

# 最近 1 小时
sudo journalctl -u qq-forward-bot --since "1 hour ago"

# 错误日志
sudo journalctl -u qq-forward-bot | grep -i error
```

## 🚨 故障排查

### 部署失败
1. 查看 Deploy workflow 日志
2. 检查语法错误或配置问题
3. 如果自动回滚失败，手动运行 Rollback workflow

### 健康检查失败
1. 查看 Health Check workflow 日志
2. SSH 到服务器检查服务状态
3. 查看服务日志排查问题

### 回滚失败
1. SSH 到服务器
2. 手动回滚：
   ```bash
   cd /home/ubuntu/Monitor
   git log --oneline -10  # 查看提交历史
   git reset --hard <commit-sha>
   sudo systemctl restart qq-forward-bot
   ```

## 💡 最佳实践

1. **部署前测试**：在本地测试代码后再推送
2. **小步快跑**：频繁小改动比大改动更安全
3. **监控日志**：定期查看 Health Check 结果
4. **备份配置**：`.env` 文件不在 Git 中，需手动备份
5. **快速回滚**：发现问题立即回滚，不要尝试修复

## 📊 部署流程图

```
推送代码到 main
    ↓
触发 Deploy workflow
    ↓
备份当前版本 → 拉取代码 → 语法检查
    ↓
重启服务 → 健康检查
    ↓
    ├─ 成功 → 部署完成 ✅
    └─ 失败 → 自动回滚 ⏮️
```

## 🔧 自定义配置

### 修改健康检查频率
编辑 `.github/workflows/health-check.yml`：
```yaml
schedule:
  - cron: '0 */6 * * *'  # 每 6 小时 → 改为其他值
```

### 修改部署超时时间
编辑 `.github/workflows/deploy.yml`：
```yaml
timeout-minutes: 10  # 10 分钟 → 改为其他值
```

### 修改服务名称
如果服务名不是 `qq-forward-bot`，修改所有 workflow 中的：
```bash
SERVICE_NAME="qq-forward-bot"  # 改为你的服务名
```

## 📚 相关文档

- [GitHub Actions 文档](https://docs.github.com/en/actions)
- [appleboy/ssh-action](https://github.com/appleboy/ssh-action)
- [systemd 服务管理](https://www.freedesktop.org/software/systemd/man/systemctl.html)
