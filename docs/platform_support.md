# Cross-Platform Support：Windows / Linux Native Backend

本文档定义 Local Knowledge Agent OS Backend Core 的跨平台支持方案。

当前目标不是维护两套后端，而是保留一套 Python 后端核心，并把 Windows、
Linux、macOS 和 WSL 的差异集中到平台适配层中。

---

## 1. 支持目标

### 1.1 原生运行目标

Backend Core 应能在以下环境中直接运行：

- Windows 10/11 + Python 3.12+ + PowerShell
- Linux + Python 3.12+ + sh/bash
- macOS + Python 3.12+ + sh/zsh

WSL 是可选运行方式，不是 Windows 支持的前提。当前项目不再维护 Docker 运行路径。

### 1.2 架构目标

```text
FastAPI API
  -> Backend Runtime
  -> Platform Adapter
       -> PathResolver
       -> FilesystemScanner
       -> CommandRunner（后续）
  -> SQLite
```

业务模块不应直接分散判断 Windows 或 Linux。路径、文件系统和命令执行差异
应优先进入 `app/platform/`。

---

## 2. 当前平台层结构

```text
app/platform/
  __init__.py
  base.py          # PlatformInfo
  detect.py        # detect_platform
  paths.py         # PathResolver / ResolvedWorkspacePath
  filesystem.py    # FilesystemScanner / ScanOptions / WorkspaceScanResult
```

当前已经接入的平台行为：

- 根据 `LKA_PLATFORM` 自动识别后端运行平台。
- 解析 frontend 传入的 workspace 路径。
- 生成规范化 workspace 路径。
- 统一扫描 workspace 文件元数据。
- 支持 `recursive`、`skip_hidden`、`allow_symlinks`、`max_files`、`sample_limit`。
- Debug retrieval 复用同一套扫描器。

后续预留但尚未实现：

- `CommandRunner`
- shell / PowerShell 命令适配
- 外部工具发现
- Windows 文件属性更完整处理
- workspace root 权限拒绝响应

---

## 3. 配置项

`.env.example` 中的平台相关配置：

```text
LKA_PLATFORM=auto
LKA_DEFAULT_SHELL=auto
LKA_WORKSPACE_ROOTS=
LKA_ALLOW_SYMLINKS=false
LKA_SKIP_HIDDEN=true
LKA_MAX_SCAN_FILES=50000
```

运行数据目录通过 `LKA_DATA_DIR` 指定，默认是 `./data/runtime`。其中只应保留个人使用的
SQLite、导入知识、邮件同步状态、运行日志、token 和本地模型缓存；测试与评测必须使用临时
目录，受版本控制的样本只放在 `evals/fixtures/`。

说明：

- `LKA_PLATFORM=auto`：由 Python 进程自动识别 `windows`、`linux` 或 `macos`。
- `LKA_DEFAULT_SHELL=auto`：后续命令执行层使用，Windows 默认 PowerShell。
- `LKA_WORKSPACE_ROOTS`：可选的 workspace 根目录限制，多个根目录用 `;` 分隔。
- `LKA_WSL_WINDOWS_MOUNT_ROOT`：Linux backend 接收 Windows 路径时的 WSL 挂载根，默认 `/mnt`；
  `C:/Users/name/project` 会映射为 `/mnt/c/Users/name/project`。
- `LKA_ALLOW_SYMLINKS`：是否允许扫描 symlink。
- `LKA_SKIP_HIDDEN`：是否跳过隐藏文件。
- `LKA_MAX_SCAN_FILES`：单次 workspace 扫描最多统计的文件数。

---

## 4. 原生运行指令

### 4.1 Linux

```bash
cp .env.example .env
uv sync
uv run python scripts/start_backend.py personal
```

Workspace 示例：

```json
{
  "workspace": "/home/chuan/Documents/NTU",
  "source_frontend": "linux-native"
}
```

### 4.2 Windows PowerShell

```powershell
Copy-Item .env.example .env
uv sync
uv run python scripts/start_backend.py personal
```

Workspace 示例推荐使用 `/`，避免 JSON 反斜杠转义：

```json
{
  "workspace": "C:/Users/chuan/Documents/NTU",
  "source_frontend": "windows-native"
}
```

也可以使用反斜杠，但 JSON 中必须转义：

```json
{
  "workspace": "C:\\Users\\chuan\\Documents\\NTU",
  "source_frontend": "windows-native"
}
```

---

## 5. API 调用差异

Linux:

```bash
curl -X POST http://127.0.0.1:8765/workspaces/index \
  -H "Content-Type: application/json" \
  -d '{"workspace":"/home/chuan/Documents/NTU","source_frontend":"linux-native"}'
```

Windows PowerShell:

```powershell
Invoke-RestMethod `
  -Uri "http://127.0.0.1:8765/workspaces/index" `
  -Method Post `
  -ContentType "application/json" `
  -Body '{"workspace":"C:/Users/chuan/Documents/NTU","source_frontend":"windows-native"}'
```

---

## 6. 差异处理原则

### 6.1 路径

- API 的 `workspace` 表示后端进程可访问的本地路径。
- Windows 原生后端直接使用 Windows 路径。
- Linux 原生后端直接使用 POSIX 路径。
- WSL 场景应通过挂载路径或后续 path mapping 处理。
- 存储层保存规范化后的后端路径，便于复查和复用。

### 6.2 文件系统

平台层统一处理：

- 隐藏文件。
- symlink 是否跟随。
- 权限错误。
- 非递归和递归扫描。
- 扫描文件数上限。
- 相对路径统一输出为 `/` 风格。

后续文本抽取阶段还需要处理：

- Windows OneDrive 占位文件。
- 锁定文件。
- 长路径。
- 编码探测。
- CRLF / LF 换行差异。

### 6.3 命令执行

后续任何本地工具、Expert Tool、测试命令、Codex / Claude Code 调用都应经过
`CommandRunner`，不要在业务代码中直接写死 `bash`、`sh` 或 `powershell`。

推荐规则：

- 优先使用 `argv: list[str]`，避免 shell 注入和平台转义问题。
- 只有确实需要 shell 语法时才进入平台 shell adapter。
- Windows 使用 PowerShell / `.cmd` / `.exe` 适配。
- Linux 使用 sh/bash 和 POSIX 可执行权限适配。
- 命令结果必须能进入 trace。

---

## 7. 测试矩阵

个人使用服务必须通过 `uv run python scripts/start_backend.py personal` 启动，默认监听
`127.0.0.1:8765`，并使用真实 `config/local.toml` 与 `data/runtime`。测试服务必须通过
`uv run python scripts/start_backend.py test` 启动，默认监听 `127.0.0.1:8766`；该模式会
强制使用临时数据目录和不存在的 local config，因此不会发起个人邮箱同步或调用真实 LLM。
需要在服务停止后检查测试数据时，显式设置 `--test-data-dir ./data/test-runtime`。

最低验证矩阵：

```text
Linux:
- uv sync
- uv run pytest
- uv run python scripts/start_backend.py personal
- GET /health
- POST /workspaces/index with POSIX path

Windows:
- uv sync
- uv run pytest
- uv run python scripts/start_backend.py personal
- GET /health
- POST /workspaces/index with C:/... path
```

当前已有测试覆盖：

- `detect_platform("windows")` 的基础识别。
- hidden 文件跳过。
- 非递归扫描。
- `/workspaces/index` 使用平台扫描配置。
- `/runtime/debug` 的结构化链路和 trace 持久化。

---

## 8. 后续重构顺序

1. 继续让 workspace 内容扫描走 `FilesystemScanner`。
2. 增加文件类型识别与文本抽取，但保持只读。
3. 增加 `CommandRunner`，为本地工具和专家工具做准备。
4. 扩展 Capability Registry，增加平台可用性字段。
5. 将 Windows/Linux smoke test 写入运行文档。
6. 只有在确有需要时才加入 WSL path mapping。
