# Violation attribution — operational assumptions

This document describes what `contracts/attributor.py` expects from the environment and from Week 4 lineage exports, and how to run it safely in **monorepos** or **non-standard layouts**.

## Git

| Assumption | Behavior if not met |
|------------|---------------------|
| `git` is on `PATH` | Attribution falls back to **synthetic** commit metadata (`0…0` hash, `unknown` author) and uses **file mtime** for age heuristics. |
| The enforcer project directory is inside a **git work tree** (or you set an override) | Same synthetic fallback for `git log` / `git blame`. |
| File paths in lineage metadata resolve under the enforcer **project root** (`_REPO`) | Producer files are still listed in `blame_chain`, but git enrichment may be skipped for paths outside the detected work tree. |

### Monorepos and subdirectory checkouts

Git commands must run with `cwd` at the **repository root**, not necessarily at the Week 7 package folder. The attributor resolves the root with:

```bash
git -C <project_root> rev-parse --show-toplevel
```

If auto-detection is wrong (sparse checkout, nested clone, or the contract enforcer lives deep under a parent repo), set:

| Variable | Purpose |
|----------|---------|
| `CONTRACT_ENFORCER_GIT_TOPLEVEL` | Absolute path to the git work tree root used for `git log`, `git blame`, and `git show`. |

Paths passed to git are **relative to that root** (computed from resolved file paths). This avoids broken attribution when the enforcer is e.g. `monorepo/services/contract-enforcer/` but git history lives at `monorepo/`.

## Lineage snapshot (“freshness”)

| Assumption | Notes |
|------------|--------|
| Default input is `outputs/week4/lineage_snapshots.jsonl` (override with `--lineage`). | The **last non-empty JSON object** in the file is used as the active graph. File **mtime** is not used for business freshness. |
| Optional field `captured_at` on that snapshot | ISO-8601-style timestamp. Used only for **warnings** in `attribution_context.lineage_snapshot.warnings`, not to reject the snapshot. |
| Optional field `git_commit` on that snapshot | Echoed into `attribution_context` for traceability; does not gate attribution. |

### Staleness warnings

| Variable | Default | Effect |
|----------|---------|--------|
| `CONTRACT_ATTRIBUTOR_LINEAGE_MAX_AGE_DAYS` | `14` | If `captured_at` parses and is older than this many days, a warning is added recommending a Week 4 re-export. Set to `0` to disable age checks. |

Warnings are also emitted when:

- No snapshot could be loaded (missing file or empty JSONL).
- `captured_at` is missing (freshness not validated).
- `captured_at` is present but **unparseable**.

## Graph path conventions

- Lineage **FILE** nodes may carry `metadata.path` or `metadata.file_path` (preferred).
- If metadata paths are empty, the attributor can derive a relative path from a `file::relative/path` **node_id** (prefix stripped, slashes normalized).

Paths are interpreted **relative to the enforcer project root** when resolving on disk (`project_root / rel`). They must match how the Cartographer (or your lineage producer) recorded paths for your layout.

## Output contract additions

Each enriched violation JSON line may include:

- **`attribution_context`**: `git_work_tree`, `project_root`, `lineage_snapshot` (`snapshot_path`, `captured_at`, `git_commit`, `warnings[]`).

Downstream tools should treat `warnings` as **non-fatal** signals; `blame_chain` and `blast_radius` still populate using BFS fallbacks when the graph is sparse.
