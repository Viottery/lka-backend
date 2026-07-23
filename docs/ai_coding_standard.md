# AI Coding Standard

This document is the coding workflow contract for work on Local Knowledge Agent OS.
It defines how the coding agent should read, confirm, edit, validate, and report.

It does not define product behavior, task semantics, or implementation scope.
Those belong to:

- `docs/project_overview.md`
- `docs/backend_engineering_guide.md`
- `docs/backend_implementation_plan.md`
- `docs/api_contract.md`
- `docs/mvp_todolist.md`

The goal here is to make implementation work predictable, reviewable, and easy to verify:

- read the project documents before making changes
- confirm expectations before changing architecture or implementation details
- report progress in small, clear steps
- always identify touched files, tests run, and validation results
- leave behind a precise summary that another developer can follow
- use `docs/backend_implementation_plan.md` as the long-term roadmap
- use `docs/mvp_todolist.md` as the current execution queue
- work one checklist item at a time, with confirmation before action
- only advance the checklist after the current item is verified
- preserve changes with git only after the item passes its checks

---

## 1. Core Principles

### 1.1 Documentation-first

Before coding, the agent must read the project’s source-of-truth documents:

- `docs/project_overview.md`
- `docs/backend_engineering_guide.md`
- `docs/backend_implementation_plan.md`
- `docs/api_contract.md`
- `docs/mvp_todolist.md`

If a task depends on broader project direction, also read `temp.md` as the original
source draft.

### 1.2 Confirm before committing to important decisions

Before starting implementation, the agent should verify the following with the user
when they are not already explicit in the docs or task:

- expected user-visible effect
- preferred implementation path
- architecture changes
- API shape changes
- data model changes
- risk tolerance for automation
- whether a feature should be stubbed or fully implemented

If a decision materially affects scope, behavior, or structure, the agent should pause
and confirm instead of guessing.

### 1.3 Prefer incremental progress

The agent should make the smallest useful change that moves the project forward.

- avoid large speculative rewrites
- keep changes aligned with the MVP plan
- preserve existing intent and terminology
- finish one coherent slice before starting the next

### 1.4 Be explicit and traceable

Every meaningful step should be visible to the user:

- what goal is being pursued
- what file(s) are being modified
- what tests are being run
- what the results mean
- how the change can be verified

---

## 2. Required Execution Flow

The agent should follow this flow for implementation work.

### Step 1: Read and orient

The agent must first identify the relevant docs and current code paths.

Minimum orientation checklist:

- understand the project goal
- understand the current architecture
- understand the MVP scope
- identify the file(s) likely to change
- identify the acceptance criteria

### Step 2: Confirm expectations

Before changing code, the agent should confirm:

- the desired outcome
- the exact checklist item to execute, if the task comes from `docs/mvp_todolist.md`
- whether the item belongs to the current execution queue or is only part of the long-term roadmap
- whether the change is documentation-only or code-affecting
- whether the user wants a stub, MVP implementation, or a fuller version
- whether any architectural or API decisions need approval

### Step 3: Plan the work

The agent should break the task into small, ordered steps:

- identify the next unchecked checklist item
- state the item to the user and wait for confirmation
- change only the files needed for that item
- run the corresponding check for that item
- fix issues only for that same item
- re-run validation for that item
- update the checklist only after the check passes
- preserve the verified change with git before moving on

### Step 4: Implement

The agent should make one coherent change set at a time.

Rules:

- keep implementation aligned with docs
- never batch multiple checklist items into one change unless the user explicitly asks for it
- use `backend_implementation_plan.md` to understand the long-term direction, but do not execute roadmap items unless they are also in `mvp_todolist.md`
- avoid unrelated cleanup unless it helps the task
- add concise comments only when they improve comprehension
- preserve existing project vocabulary

### Step 5: Validate

The agent should run the most relevant validation available:

- syntax checks
- unit tests
- targeted smoke tests
- API calls
- basic runtime startup

If full tests are not available, the agent should say so clearly and explain what was validated instead.

### Step 6: Report

The final report should summarize what changed and how it was verified.

---

## 3. Pre-Implementation Confirmation Checklist

Before coding, the agent should confirm the following when relevant:

- What user-facing result should this produce?
- Is this meant to be a documentation update, an MVP stub, or a production-like implementation?
- Are we allowed to change architecture, or should we stay within the current structure?
- Should the agent preserve existing terms from the project docs?
- Are there any files, folders, or behaviors that must not change?
- What does success look like for this task?

If the answer is unclear and materially affects the work, the agent should ask.

---

## 4. Output Reporting Standard

Every progress update and final response should be structured around the same questions.

### 4.1 Progress update format

Use short, concrete status updates while working.

Each update should cover:

- current goal
- what was just learned
- what is being changed next
- whether any risk or ambiguity was found

### 4.2 Final delivery format

The final response should include:

1. What was done.
2. Which files changed.
3. What tests were run.
4. What the result was.
5. How to verify the change.

### 4.3 Required change summary for each task

For each completed task, the agent should clearly state:

- objective
- modified files
- implementation summary
- tests run
- validation result
- any known limitations
- whether the related checklist item was marked complete and saved

---

## 5. File Change Rules

### 5.1 Before editing

The agent should identify the exact files that will be touched and why.

### 5.2 During editing

The agent should keep changes scoped to the requested task.

- do not silently rewrite unrelated modules
- do not remove project intent from docs
- do not simplify away important design language

### 5.3 After editing

The agent should review the diff mentally or with tooling and confirm:

- the change matches the request
- the change is consistent with the docs
- the change does not introduce avoidable confusion

---

## 6. Testing and Validation Rules

### 6.1 Always test when behavior changes

If code changes affect runtime behavior, the agent should run at least one relevant validation step.

Preferred validation order:

- syntax or compile check
- targeted unit test
- targeted smoke test
- API route check
- startup verification

### 6.2 Report test scope honestly

If the agent cannot run a certain test, it should say so directly.

The report should state:

- what was actually run
- what was not run
- whether the remaining risk is low, medium, or high

### 6.3 Verification should be reproducible

The agent should explain how another person can reproduce the check.

Examples:

- command to run
- endpoint to call
- file to inspect
- expected output shape

---

## 7. Architecture and Scope Guidance

### 7.1 Keep architecture aligned with the project vision

The project’s target architecture is:

- backend core
- multiple frontends
- Knowledge Context Engine
- Capability Registry
- Native Skills
- Local Tools
- Expert Tools
- verifier
- trace recorder
- skill evolution layer
- task
- plan
- confirmation
- skill

The agent should prefer changes that support this architecture rather than replacing it.

### 7.2 Do not overbuild the MVP

For MVP work, the agent should prefer:

- simple deterministic behavior
- stable interfaces
- clear data models
- traceability
- explicit confirmation for risky actions

Overly complex automation should be deferred unless the task explicitly asks for it.

### 7.3 Preserve project language

Use the project’s established terms consistently:

- Main Agent Brain（主代理大脑）
- Knowledge Context Engine（知识上下文引擎）
- Capability Registry: the authoritative catalog of available capabilities, their metadata, and confirmation requirements.
- Native Skills
- Local Tools
- Sub Agents
- Expert Tools
- MCP Tools
- Verifier
- Trace Recorder
- Task
- Plan
- Confirmation
- Skill
- Skill Evolution Layer

---

## 8. Suggested Interaction Pattern With the User

When starting implementation, the agent should ideally state:

- what part of the docs it used to understand the task
- what outcome it will aim for
- what files are likely to change
- what it will test afterward

When a decision is ambiguous, the agent should ask a focused question instead of assuming.

When work is done, the agent should provide a concise but complete report that answers:

- what changed
- why it changed
- how to verify it

---

## 9. Minimal Reporting Template

Use this template for task completion reports:

```text
目标:
- ...

修改文件:
- ...

做了什么:
- ...

测试:
- ...

结果:
- ...

如何验证:
- ...

备注:
- ...
```

---

## 10. How This Standard Should Be Used

This file is the working contract for coding tasks in this repository.

Before implementing anything meaningful, the agent should read this file and the
project docs together. When in doubt, prefer:

1. clarity over cleverness
2. traceability over hidden automation
3. incremental progress over risky rewrites
4. user confirmation over assumptions
