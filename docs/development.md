# Reliable-MiniSWE development workflow

This fork keeps the original mini-swe-agent history while developing the
Reliable-MiniSWE extensions on a dedicated branch.

## Remotes and branches

- `origin` points to the personal fork:
  `git@github.com:jupiter12334/reliable-mini-swe-agent.git`. Push project work
  only to this remote.
- `upstream` points to the official repository:
  `https://github.com/SWE-agent/mini-swe-agent.git`. Use it only to fetch
  official updates; do not push project work to it.
- `reliable-mini-swe-agent` is the dedicated development branch for the fork.
- `main` mirrors the upstream line and is not used for feature development.

Verify the local setup with:

```bash
git remote -v
git branch -vv
git status --short --branch
```

If the official remote is missing, add it once:

```bash
git remote add upstream https://github.com/SWE-agent/mini-swe-agent.git
git fetch upstream
```

## Daily development

Start work from the dedicated branch:

```bash
git switch reliable-mini-swe-agent
git status --short --branch
```

Keep commits scoped to the Reliable-MiniSWE work and push the branch to the
personal fork:

```bash
git push origin reliable-mini-swe-agent
```

## Synchronizing official updates

Fetch first so reviewing upstream changes does not modify the worktree:

```bash
git fetch upstream
git log --oneline --decorate reliable-mini-swe-agent..upstream/main
```

When the upstream changes are understood, merge them into the development
branch:

```bash
git switch reliable-mini-swe-agent
git status --short --branch
git merge upstream/main
```

Resolve conflicts only after identifying which side owns each change. After a
successful merge, run the repository checks before pushing:

```bash
pytest
git status --short --branch
git push origin reliable-mini-swe-agent
```

Do not run `git pull upstream main` from an arbitrary branch. The explicit
fetch-and-merge sequence keeps the synchronization target visible and makes
the upstream merge auditable.
