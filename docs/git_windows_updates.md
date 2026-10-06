# Git 管理与另一台 Windows 的源码更新

日期：2026-10-06。当前先完成后端源码的 Git 整理；不创建远程仓库、不推送，
不自动更新或重启任何运行实例。预算和模型配置保持原状。

## 提交边界

- 提交应用源码、测试、合成 fixture、依赖锁文件、部署脚本和开发文档。
- 只提交无凭据的 `.env.example`、`config/*.example.toml` 配置模板。
- 不提交真实配置、API 密钥、邮箱授权、数据库、会话、记忆、日志、模型缓存、
  私人便携包或虚拟环境。`.gitignore` 不是凭据扫描器，也不会自动移除已跟踪文件；
  提交前仍须检查暂存清单，发布前检查历史。
- 用户自己的指导文件与关注项保留在本地数据目录；仓库的 `AGENTS.md` 是项目开发
  规范，不是用户偏好或账号配置。
- 现有完整便携包包含真实授权，只能私下迁移，不作为 GitHub Release 附件。

当前工作区尚无 Git remote。配置远程仓库和首次推送是后续单独操作。
前端、QQ 采集器和桌宠是独立项目，不随本后端仓库自动更新。

## 新电脑的源码安装

目标电脑已有 Git，还需要原生 Python 3.12+。在 Windows PowerShell 中：

```powershell
git clone <你的仓库地址> lka_backend
Set-Location .\lka_backend
git switch <发布分支>
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup_windows.ps1
```

尖括号内容应替换为实际值。安装脚本按 `uv.lock` 同步依赖，只在缺失时生成本地配置，
不覆盖已存在的配置。首次生成的模板不是当前机器的真实模型和邮件配置，需在新电脑
单独配置、授权，并调整工作区路径；Windows DPAPI 凭据不可直接当作跨机器配置复制。

不要在运行中的便携包 `backend` 目录直接 `git clone` 或覆盖文件。便携包有自己的
Python、路径重定位与校验清单；Git 源码安装和便携包安装暂时是两条不同的维护路径。

## 日常更新

更新前，确认位于正确的源码安装目录，工作树没有本地代码改动：

```powershell
git status --short
git fetch origin
git log --oneline HEAD..origin/<发布分支>
git diff --stat HEAD..origin/<发布分支>
```

有本地改动则先保留、审阅并合并；不要使用 `reset --hard`、强制覆盖或盲目 stash。
建议只发布经过验证的提交或 tag，不把正在开发的工作树视作稳定版本。

准备更新：

1. 记录旧提交：`git rev-parse HEAD`。等待 Agent 和后台任务完成，再停止对应实例。
2. 在当前代码与虚拟环境仍可用时，备份代码和本地配置；对 SQLite 使用 backup API，
   或确认全部写入进程已停止后备份完整数据库，不能只复制仍在写入的主库文件。
3. 普通原生安装可用 `production_release.py snapshot --target <安装目录>` 备份代码。
   它**不备份业务数据库**，数据库备份仍须单独完成。
4. 从当前发布分支执行下面的 fast-forward 更新与依赖安装：

```powershell
git pull --ff-only origin <发布分支>
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup_windows.ps1
```

5. 启动目标实例，检查 `/health`、`git rev-parse HEAD`、模型配置及基础工具功能。
   正确的启动/停止方式见 [Windows 操作手册](windows_operations.md)；后端独立重启脚本
   依赖配套前端的运行记录与 QQ outbox 检查，不是适用于所有安装的通用停服命令。

当前 `deployment_id` 来自部署清单，普通 `git pull` 不会自动刷新清单，不能将旧清单
标识作为新提交已加载的证明。健康检查也不等于全部业务功能通过验收。

升级失败时保留诊断和升级后的数据。依赖锁文件及数据库格式可能已经变化，
不能只切回旧源码就认定完成回滚；恢复旧数据库前必须明确可能丢失升级后的写入。

## 本次整理与验收

这次将此前积累的 message 阅读/恢复、上下文与记忆优化、原生 Windows 支持和相应
测试文档作为一个集成检查点提交，不人为拆分存在依赖的运行时改动。

验收包括：提交路径检查、常见凭据模式扫描（命中项人工区分合成测试数据）、
Python 语法检查、Git whitespace 检查、配置排除回归、部署及 message 恢复相关离线测试。
这些检查不代表全套业务回归或第二台物理电脑已验收；本次不调用付费模型。

本次结果：175 个提交路径均属于源码、测试、文档、脚本或无凭据模板；未跟踪真实
配置或运行数据。检查 139 个新增/修改 Python 文件的语法；143 项现有离线回归通过，
18 项提交边界测试通过，新增测试的 Ruff 检查与暂存 diff whitespace 检查通过。
提交边界测试首次暴露了 Git 将冒号开头的测试路径按特殊路径语法解释的问题，
以 `./` 明确相对路径后复测通过；本地 `:memory:.ses` 文件保留但不提交。

常见凭据模式扫描在暂存内容中只命中一个已核对的合成测试占位值；不将模式扫描
等同于完整的隐私审计。真实配置路径的历史检查未发现提交记录，没有重写 Git 历史。

后续如需要一键更新，可将上述步骤封装成 Windows 脚本；目前尚未实现自动更新器、
GitHub 发布流程或跨电脑数据同步。
