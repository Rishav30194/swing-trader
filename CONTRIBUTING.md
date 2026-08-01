# Contributing Guide

## Branching Strategy

`main` is the stable branch. **Never push directly to `main`.** All changes go through a feature branch and a pull request.

### Branch Naming

```
<type>/<short-description>
```

| Type      | When to use                                      | Example                          |
|-----------|--------------------------------------------------|----------------------------------|
| `feature` | New functionality or phase work                  | `feature/phase-2-indicators`     |
| `fix`     | Bug fix                                          | `fix/trailing-stop-calc`         |
| `chore`   | Config, deps, docs, tooling — no logic change    | `chore/update-requirements`      |

### Workflow

```bash
# 1. Create a branch from main
git checkout main && git pull
git checkout -b feature/your-feature-name

# 2. Work, commit, push
git add <files>
git commit -m "short description of what and why"
git push -u origin feature/your-feature-name

# 3. Open a PR on GitHub → merge into main
# 4. Delete the feature branch after merge
```

---

## Versioning

Versions are **milestone-based tags** — not created on every commit. Only tag when something meaningful ships.

### Version Scheme

```
v<MAJOR>.<MINOR>.<PATCH>
```

| Segment | Bump when                                                 |
|---------|-----------------------------------------------------------|
| MAJOR   | Live capital deployment begins (`v1.0.0`)                 |
| MINOR   | A phase is completed or a significant feature ships       |
| PATCH   | A meaningful bug fix that warrants a named checkpoint     |

### Planned Milestones

| Tag      | Milestone                                      |
|----------|------------------------------------------------|
| `v0.1.0` | Phase 1 complete — data pipeline validated     |
| `v0.2.0` | Phase 2 complete — backtest validates strategy |
| `v0.3.0` | Paper trading validated                        |
| `v1.0.0` | Live capital deployment                        |

### How to Cut a Release

```bash
# Tag the current state of main
git checkout main && git pull
git tag -a v0.2.0 -m "Phase 2 complete: indicators and backtest validated"
git push origin v0.2.0

# Then create a GitHub Release from the tag (optional but recommended)
gh release create v0.2.0 --title "v0.2.0 — Phase 2 complete" --notes "Backtest Sharpe > 0.5, max drawdown < 25% across 2022–2024."
```
