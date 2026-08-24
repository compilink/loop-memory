# Loop Memory

> Every conversation with an AI begins as a new loop—a fresh context, a new
> attempt to understand the work before us. Code may persist, but intentions,
> judgments, and the quiet lessons between decisions are easily lost. Loop
> Memory gives those lessons a local place to endure, so the next loop can
> begin again without beginning from nothing.
>
> 与 AI 的每次对话，都是一次全新的轮回：新的上下文，也是对眼前工作的新一次理解。代码可以留存，但意图、判断，以及一次次取舍间那些安静的经验，很容易随会话消散。Loop Memory 为这些经验留下一处本地归宿，让下一次轮回依然从新开始，却不必从零开始。

Codex-only installer for the shared Loop Engineering methodology and local
Loop Memory runtime on macOS and Linux.

## Why Loop Memory / 为什么需要 Loop Memory

AI agents are powerful, but every session is finite. Context is compressed,
tasks are handed off, tools change, and even a capable agent can arrive in a
project without knowing why yesterday's choices were made.

AI Agent 很强大，但每一次会话都有终点。上下文会被压缩，任务会被交接，工具也会变化；即使能力出众的 Agent，也可能再次来到一个项目时，已经不知道昨天为何做出那些选择。

Loop Memory does not try to preserve every word. It keeps only what deserves
another life: reusable methodology, verified project knowledge, and the
smallest handoff needed to resume meaningful work. Memory becomes a compass
rather than a chain.

Loop Memory 并不试图保存每一句话。它只留下那些值得进入下一次轮回的内容：可复用的方法论、经过验证的项目知识，以及恢复有意义工作所需的最小交接。记忆因此成为罗盘，而不是锁链。

Each task is still allowed to be a new loop. The agent can question old
assumptions, verify the present, and choose a better path—but it no longer has
to rediscover every hard-won lesson alone.

每个任务依然可以是一次全新的轮回。Agent 可以质疑旧有假设、验证当下事实，并选择更好的道路；只是它不必再独自重新发现每一条来之不易的经验。

## Requirements

- macOS or Linux
- Python 3.11 or newer
- Codex installed for the current operating-system user

## Install

```bash
git clone --depth 1 https://github.com/compilink/loop-memory.git
cd loop-memory
python3 install.py
```

The installer preserves existing Codex configuration, installs one managed
Loop Engineering block in `~/.codex/AGENTS.md`, and initializes
`~/loop-memory` only when it does not already exist.

If the result contains `codex_trust_review=required`, open `/hooks` in Codex,
review the three Loop Memory lifecycle commands, and approve them once.

Re-run `python3 install.py` to converge to the checked-out version.

## Upgrade

After updating an existing checkout, upgrade the installed Runtime, Skills,
and managed Codex configuration with:

```bash
git pull --ff-only
python3 install.py --upgrade
```

Upgrade requires an existing installer manifest. It verifies that the
currently managed Runtime, launcher, and Skills have not been locally modified,
then uses the same transactional publish, verification, and rollback path as
installation. Missing managed targets are restored, newly introduced Skills
can be added, and `~/loop-memory` is always preserved. If the result contains
`codex_trust_review=required`, open `/hooks` in Codex and approve the changed
lifecycle commands before treating hook acceptance as complete.

## Uninstall

```bash
python3 install.py --uninstall
```

Uninstall removes only installer-owned Runtime, Skills, configuration entries,
hooks, and the managed AGENTS block. It never deletes `~/loop-memory`.

## Data boundary

The repository contains reusable methodology, runtime code, synthetic tests,
and three Skills: `managing-loop-memory`, `governing-subagents`, and
`governing-task-scope`. It does not contain or export any user's project, session,
fact, registry, migration, or legacy memory data.
