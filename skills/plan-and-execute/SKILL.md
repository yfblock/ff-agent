---
name: plan-and-execute
description: 用户提出复杂任务、多步开发、排查问题、批量文件修改或需要分阶段推进时使用；也适用于用户明确要求「先规划再执行」。
---

# 规划与执行

当任务**不是**一两句话能完成时，使用规划 + 逐步执行，而不是一次性给出空泛建议。

## 何时需要计划

- 涉及多个文件或多次命令验证
- 需要先调研再改动
- 步骤之间有依赖（先读后改、先测后修）
- 用户说「帮我实现」「逐步完成」「排查并修复」

简单问答、解释概念、单次 `/memory add` 等**不要** create_plan。

## 标准流程

1. **create_plan**：拆成 3–8 步，每步 id 唯一、description 可执行
2. 对当前步骤：**update_plan_step(status=in_progress)**
3. 使用执行工具完成该步：
   - **read_file** / **list_directory** — 了解现状
   - **write_file** — 修改或创建文件
   - **run_command** — 运行测试、构建、git 等（工作区内）
4. **update_plan_step(status=completed, result=...)** 记录结果
5. 重复 2–4 直到全部完成
6. **complete_plan(summary=...)** 后向用户汇总

## 步骤状态

| 状态 | 含义 |
|------|------|
| pending | 未开始 |
| in_progress | 正在执行（同时只应有一个） |
| completed | 成功完成 |
| failed | 失败，需在 result 说明原因 |
| skipped | 因前置结论而跳过 |

## 注意

- 操作限定在配置的工作区（`.env` 中 `WORKSPACE_DIR`）
- 失败时不要假装完成；标记 failed 并调整方案
- 用户可用 `/plan` 查看进度，`/plan clear` 取消计划
- **进行中的计划会自动保存**；异常退出或重启后会恢复，可用 `/plan` 确认后继续执行
