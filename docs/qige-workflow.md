# Qige Fork Workflow

这份文档记录当前 `hermes-agent` fork 的长期自用维护方式。

目标：
- `main` 尽量贴近官方上游
- `qige` 作为长期自用分支
- 日常使用和自定义改动都落在 `qige`
- 需要时再把官方更新从 `main` 合并到 `qige`

## 当前仓库结构

- `origin`：`git@github.com:caiwenhao/hermes-agent.git`
- `upstream`：`https://github.com/NousResearch/hermes-agent.git`
- `main`：用于同步官方主线
- `qige`：长期自用分支

查看当前状态：

```bash
git remote -v
git branch -vv
```

## 日常使用

平时直接工作在 `qige`：

```bash
git checkout qige
git pull origin qige
```

如果只是本地跑 Hermes、继续加自己的默认配置、偏好能力、工具链适配，默认都在 `qige` 上做。

## 同步官方主线

先把官方最新改动同步到本地 `main`，再推到自己的 fork：

```bash
git checkout main
git fetch upstream
git merge upstream/main
git push origin main
```

说明：
- `upstream` 是官方仓库
- `origin` 是自己的 fork
- `main` 应尽量保持接近官方，不要长期堆个人定制

## 把官方更新带到 qige

当 `main` 同步完官方后，把这些更新合并到长期自用分支：

```bash
git checkout qige
git merge main
git push origin qige
```

推荐使用 `merge`，而不是长期对 `qige` 用 `rebase`。

原因：
- 更稳
- 更适合长期自用
- 冲突处理更直观
- 不容易改乱已推送历史

## 新功能开发建议

如果后面要做较大改动，建议从 `qige` 再切功能分支：

```bash
git checkout qige
git pull origin qige
git checkout -b feat/some-change
```

完成后合回 `qige`：

```bash
git checkout qige
git merge feat/some-change
git push origin qige
```

## 这次浏览器默认接管方案归属

当前以下能力已经落在 `qige`：

- live CDP 默认 backend = `playwright-cdp`
- 优先复用已有目标 tab
- 命中已匹配页面时避免不必要 `goto()`
- `/browser connect` 持久化写入 profile `.env`
- `/browser disconnect` 同步清理持久化 env
- 针对 `browser_tool` 的测试补齐

## 推荐操作顺序

### 每次准备吸收官方更新时

```bash
git checkout main
git fetch upstream
git merge upstream/main
git push origin main

git checkout qige
git merge main
git push origin qige
```

### 每次准备做自己的新改动时

```bash
git checkout qige
git pull origin qige
```

### 查看是否有脏工作区

```bash
git status --short
```

如果存在不相关文件改动，不要顺手一起提交到 `qige`。

## 原则

1. `main` 负责跟官方
2. `qige` 负责你自己的长期可用版本
3. 大改动先开功能分支，再合回 `qige`
4. 不相关脏文件不要混进长期分支提交
5. 优先低风险、可回退、可持续同步的方式维护 fork
