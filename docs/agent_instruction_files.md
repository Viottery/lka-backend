# Agent 指导文件与关注指导文档

## 文件与作用域

- `data/instructions/AGENTS.md`：系统全局指导，所有 Agent turn 加载。实际位置由配置的 `data_dir` 决定；首次启动自动建立简短模板。它不是 Codex 自身的 `~/.codex/AGENTS.md`。
- `<workspace>/AGENTS.md`：项目指导。会话选中工作区时，从允许的 workspace root 到当前工作区逐层读取同名文件；越靠近当前工作区越具体。当前仓库的根 `AGENTS.md` 已存在，也会被我们的运行时读取（前提是该目录处于配置的 workspace root 内）。
- `data/instructions/watches/AGENTS.md`：所有定时关注的长期指导，首次启动自动建立。适合写证据可靠性、时间新鲜度、变化重要性和不确定性处理偏好；每条关注的目标、时刻及授权来源仍存于 Watch 定义，不写进这里。

系统在每次模型提示词装配时按“全局 → 项目根 → 当前工作区”顺序检查文件。文件可以自由排版，不要求标题、目录或固定格式。系统用修改时间、状态变更时间、大小和文件标识检测变更；检测到变化或通过工具更新后，重新构建 UTF-8 分块索引、文件指纹和抽取式摘要。未变化时复用索引；同一 Agent turn 内编辑后，下一次模型调用就会看到更新后的摘要。上下文携带每份文件的路径、前 4 KiB 预览、摘要、索引概览、分块数量和续读位置；摘要只是导航，不保证覆盖全部规则。为了控制 prompt 大小，预览和摘要有合计预算，但文件条目保留，模型可搜索索引并按字节偏移继续读取原文。一个文件超过 16 KiB 不再被跳过或导致运行失败。普通会话和多 Agent 子会话都走相同装配路径；子会话只继承其明确选定的工作区，不继承父会话历史。关注任务在每次 occurrence 执行时另外载入关注指导文件，依然每次推送新建一个会话，不为每条关注创建固定会话。

`AGENTS.md` 是运行指导，不是权限定义。它不能扩大 `ToolView`、数据源、账户范围、预算或绕过安全审查。工具结果和网页正文仍是证据而非指导。建议保持文件简短、长期有效，把大段任务资料留在知识库或工具检索结果中；文件内容可随下次 turn / occurrence 生效，不需要重启。

## Agent 如何维护

指导文件是用户维护的规则，和后台学习的派生记忆分开。普通偏好表达、纠正或撤回记忆
不等同于要求改写 `AGENTS.md`；启用后的后台记忆机制负责这类对话学习。只有用户明确
要求编辑持久指导文件时才使用更新工具，保留无关规则。此区分写在工具包元数据与工具
描述中，不在 Agent core 硬编码意图。`instructions.update` 还在工具侧核对本轮已持久化的
用户消息：仅接受明确的指导文件编辑请求；普通偏好、引用、疑问和无当前来源的调用拒绝，
子 Agent 不能借该入口改写全局指导。这个保守目标检查不代替执行器的权限与安全审查；
含糊编辑请求可能需要澄清，并不是任意自然语言授权识别器。

明确要求立即保存偏好时可用 `memory.remember(evidence)`，`evidence` 必须精确匹配本轮
用户原文中的完整、受本地提取支持的断言；不能使用网页资料、模型概括或剪掉条件的片段。
回执分别报告记忆状态（active/candidate/retracted 等）与 `MEMORY.md` 同步状态，不能把
candidate 或文件冲突称为已生效。普通对话仍走后台学习；此工具不新增模型提取调用。

全局默认模板也遵守此区分。启动时只自动升级内容完全未改动的旧默认模板；已追加偏好、
手工规则或其他内容的文件保持原样，须核对原请求与目标后单独迁移，避免覆盖用户数据。

`instructions.search(kind="global"|"watch", query)` 和 `instructions.search_project(path, query)` 查找相关分块并返回偏移量。`instructions.read(kind, offset)` 与 `instructions.read_project(path, offset)` 每次最多返回 16 KiB，按 `next_offset` 续读直到 `truncated=false`；它们不会因源文件总长度而拒绝读取。项目工具只允许读取当前工作区指导文件链中的 `AGENTS.md`。`instructions.read` 还返回完整文件的 SHA；`instructions.update(kind, content, expected_sha256)` 以完整新内容替换，只有 SHA 匹配且单次更新不超过 1 MB 时成功；更新先写临时文件再原子替换。更大的用户文件仍可分页读取，但目前不能通过这个整文件替换工具一次写入。更新工具标记为非只读，经过既有安全审查门并留下工具审计记录。关注任务可使用只读指导检索工具，不能自改指导文件；用户可在普通会话中明确要求 Agent 更新。项目文件继续通过已有 `filesystem.edit_file` 修改，也受工作区和安全审查约束。可直接在本地用编辑器修改这三类文件；下次 turn 或 occurrence 会检测变更并重建索引。

Codex 官方的文件发现方式也是分层注入：从全局目录及仓库路径上的 `AGENTS.md` 读取，按父到子顺序提供给模型；我们的实现借鉴该作用域思想，但加载范围、大小限制和工具权限由本项目代码定义，并非自动继承 Codex CLI 的行为。参考 [OpenAI 官方说明](https://developers.openai.com/api/docs/guides/latest-model?gallery=open&galleryItem=trivia-quiz-game&model=gpt-5.3-codex&translationFallback=de-DE)。
