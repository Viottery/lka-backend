# Windows 便携包交付

完整操作步骤、配置位置及后续重新打包见 [Windows 操作手册](windows_operations.md)。

日期：2026-10-04。目标：另一台 Windows x64 电脑解压即可启动原生前后端和 Java 桌宠。
保留模型、邮件及其他应用配置和必要密钥；不复制会话、记忆、邮件正文、工作区、
日志、数据库、测试或评测内容。当前电脑的运行实例不受打包影响。

## 实施 TODO

- [x] 盘点原生 Python / Java 依赖、前后端配置和客户端偏好。
- [x] 确认邮件有效配置仍在 Linux 侧，仅提取配置与授权信息。
- [x] 实现应用和运行环境打包，排除使用数据和绝对路径依赖。
- [x] 提供启动、停止、校验工具及中文说明。
- [x] 保留数据库里的全局设置，首次启动通过配置接口导入。
- [x] 在独立迁移目录、不同端口验证运行环境、服务和桌宠。
- [x] 校验压缩包内容、完整性与数据排除规则，记录交付位置。

## 打包内容与实现

- `scripts/build_windows_portable.py` 从已安装的原生 Windows 应用生成私人便携包，
  不下载依赖、不复制数据库或业务目录。保留前后端配置/密钥、Outlook 授权、全局
  UI/记忆后台配置、全局指令及桌宠偏好；工作区路径重定位到包内 `workspaces`。
- Linux 侧仅提供原有有效邮件配置、必要凭据和预训练模型资源。模型缓存的软链接
  先展开为普通文件，再移除重复 blobs；模型权重不属于会话/邮件等使用数据。
- 自带 Python 3.12、相互隔离的运行依赖、Java 21、Windows JavaFX/LWJGL 原生库、
  已编译桌宠，以及 ONNX Runtime 需要的 MSVC DLL。保留第三方包自带的许可文件。
- `scripts/windows_portable/` 提供路径重定位、首启配置导入、启动/停止、校验与说明。
  PowerShell 5.1 启动脚本无需管理员、Gradle、WSL 或系统 Python/Java。
- 未携带 QQ 客户端/登录会话；接入参数和已找到的外部桥接配置保存在 `settings`。
  外部 Codex 仍需要目标电脑独立安装/认证。

## 验证记录

验证副本放在 `D:\LKA-Releases\validation\另一台电脑 portable\LKA`，使用
8875/8890 端口；交付目录与原运行实例保持独立。验证副本单独关闭自动邮箱/QQ接收，
交付包保留原邮件配置，不宣称验证了真实邮箱连接或第二台物理电脑。

- 包内 Python 的 `sys.base_prefix`、虚拟环境、导入的 `app` 均指向新目录。
- 前后端健康检查、原生 Chrome 页面、模型配置和包内工作区创建通过。
- Java 桌宠启动诊断 `DONE ok=true`，动画资源正常载入；修复了 PowerShell 5.1
  解析 profiles JSON 时多余数组包装导致两个资源路径拼接的问题。
- 真实本地 embedding 输出 512 维向量，reranker 完成两条候选的排序，无需下载。
- 通过 `GetModuleFileNameW` 确认 MSVC DLL 从包内加载，未依赖系统安装。
- SQLite 完整性通过，会话、消息、运行、邮件、知识、项目、记忆七项业务计数均为零。
- 重复启动复用三个进程；停止脚本只停止验证副本，原 8765/8780 服务仍健康。
- 测试/评测未迁移；检查脚本与运行证据保留在忽略的 scratch 和 Windows validation 中。

- 补充验证真正的 JavaFX WebView 聊天页面，使用全新包内原生缓存加载 WebKit，
  页面脚本初始化成功。JavaFX 所需的 C++ DLL 来自包内运行库/资源，而非既有用户缓存。
- 最终 PowerShell 5.1 AST、Python 编译与 Git whitespace 检查通过。

## 使用与交付

Windows 文件位置：`D:\LKA-Releases\LKA-Windows-x64-20261004.zip`。
复制到另一台 Windows 10 1809+ / Windows 11 x64，完整解压后双击 `Start-LKA.cmd`；停止用
`Stop-LKA.cmd`；首次启动前可用 `Verify-Package.cmd` 检查内容。
包内 `README.txt` 说明配置位置、可改端口、邮箱授权与外部 QQ/Codex 的边界。
此包包含真实 API 密钥与邮箱授权，是私人迁移备份，不应公开分享。

最终归档：1251932830 bytes（约 1.25 GB / 1.17 GiB），9653 个归档条目。
逐文件解压读取并核对 SHA256 全部通过，业务数据库条目为 0。

SHA256：`5122b3c5cf5d9eac00f18177044fadcbe0f1dae1ec8162057ca319a7735c19f7`。

正式解压目录、ZIP、`.zip.sha256` 和 `release-report.json` 保留在 `D:\LKA-Releases`。
用户明确授权后，已清理 7 个中转/失败构建/验证临时目录，保留正式 ZIP、解压目录、校验文件及简洁报告；验证副本的服务均已停止，原应用继续运行。
