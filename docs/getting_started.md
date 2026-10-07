# 安装与开始使用

LKA 后端支持 Windows 和 Linux 原生 Python，不需要 Docker。
聊天界面、桌宠及 QQ 采集器在独立项目中维护，克隆后端不会同时安装它们。

在以 WSL 为主环境的开发电脑上，按下方 Linux 步骤启动日常后端；
Windows 后端副本仅做独立功能验证，流程见 [WSL 与 Windows 验证说明](maintenance.md)。
Windows 前端和采集器可以继续使用，不必把界面迁入 Linux。

## 准备环境

| 平台 | 需要准备 |
| --- | --- |
| Windows | Python 3.12+、Windows PowerShell 5.1；后台终端需要 Windows 10 1809+ / Windows 11 或 Server 2019+ |
| Linux | Python 3.12+、uv、bash |

使用 Git 时，将仓库克隆到有写入权限的目录。以下命令都从仓库根目录执行。
实际仓库地址由发布者提供；本说明不假设远程仓库或公开安装包已经存在。

## Windows

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup_windows.ps1
```

脚本使用现有 uv；没有时在项目的 `.bootstrap` 中安装，按 `uv.lock` 准备 `.venv`。
只在缺失时生成 `.env` 和 `config/local.toml`，不会覆盖已有配置。
需要指定原生 Python 时，追加 `-Python 'C:/Path/To/python.exe'`。

在本地填写模型连接与密钥，参见 [配置说明](configuration.md)，然后启动：

```powershell
.\.venv\Scripts\python.exe scripts\start_backend.py personal
```

在另一窗口检查服务：

```powershell
Invoke-RestMethod http://127.0.0.1:8765/health
```

## Linux

```bash
uv sync --locked
test -f .env || cp .env.example .env
test -f config/local.toml || cp config/local.example.toml config/local.toml
```

填写模型配置后启动：

```bash
uv run python scripts/start_backend.py personal
```

在另一终端检查服务或开始对话：

```bash
uv run lka health
uv run lka chat --session-id my-assistant
```

命令行客户端默认使用流式回复，同一 `session_id` 保留对话。

## 第一次配置

模板默认使用 mock 模型，适合确认启动流程。真实回答需要在 `config/local.toml`
接入模型，邮箱、搜索和消息来源也需要分别配置或授权。
多 Agent、邮件专家及外部代码专家为可选能力，不会因安装自动启用。

默认服务地址是 `http://127.0.0.1:8765`，数据目录是 `data/runtime`。
前台启动的服务用 Ctrl+C 停止。新配置通常需要重启才能加载。

## 聊天界面与桌宠

准备对应前端及其 Python / Java 依赖后，使用前端提供的原生启动器。
后端的 Python 环境不能代替前端或桌宠环境。
如使用私人便携包，保持包内目录结构，按随包的 `Start-LKA.cmd`、`Stop-LKA.cmd`
与说明操作，不把 Git 源码直接覆盖到运行中的便携包。

## 下一步

- [配置模型与数据连接](configuration.md)
- [使用会话、项目、记忆与关注](usage.md)
- [更新、备份与故障检查](maintenance.md)
- [接入自己的客户端](api_contract.md)
