# Evidence retention — where it runs, and why not in the fleet

`tools/prune_evidence.py` applies the retention policy for browser evidence. This note exists so the
next session does not wire it into `fleet_runner` and conclude it is broken.

## The constraint that decides the design

**The evidence root is on the host, not in a container.** The browser worker writes to
`/var/lib/granada/browser-evidence` as a host process (driven over the bridge at `172.18.0.1:8765`).
No compose service mounts that directory.

Consequence: a sweep added to `fleet_runner` would scan an empty path inside the worker container and
report `runs scanned: 0`. That is the same class of failure this codebase has recorded repeatedly —
a check that looks healthy because it is looking at nothing. So retention is **not** in the fleet
loop, and that is a decision rather than an omission.

## Running it

```bash
cd ~/granada/Auth/backend

# Plan only. Changes nothing. Safe to run as often as you like.
python3 tools/prune_evidence.py --root /var/lib/granada/browser-evidence

# Actually delete, after reading the plan.
python3 tools/prune_evidence.py --root /var/lib/granada/browser-evidence --apply
```

Defaults: keep everything for **30 days**, keep a **4-frame sample** for **90 days**, then release.

## Suggested schedule (host cron, not compose)

A nightly dry run that writes its plan to a log is enough to notice growth; the `--apply` should stay
a deliberate act until somebody has watched the plans for a week.

```cron
# 03:20 nightly — plan only, append to a log
20 3 * * * cd /home/granada/granada/Auth/backend && \
  python3 tools/prune_evidence.py --root /var/lib/granada/browser-evidence >> \
  /var/log/granada/evidence-retention.log 2>&1
```

## What was measured

Against the real store (2026-10-17, 39 runs, 485 files, 250 MB):

| Projection | Downsampled | Released | Kept | Freed |
|---|---|---|---|---|
| today | 0 | 0 | 485 | 0 MB |
| +40 days | 39 | 0 | 156 | 155.6 MB |
| +100 days | 0 | 39 | 0 | 248.6 MB |

The +40-day row is the useful one: thinning alone removes roughly 62% of the bytes while keeping the
first and last frame of every run.

## Safety properties (pinned by `tests/test_evidence_retention.py`)

- `plan_prune` is read-only; only `apply_prune` unlinks.
- `apply_prune` resolves every path and **refuses** anything outside the root — a `PrunePlan` is data
  a caller could construct by hand, so it is not trusted.
- Symlinks are neither followed at scan time nor deleted at apply time.
- The first and last frame of a run always survive down-sampling.
- A byte cap releases the oldest runs first and can never touch a run inside the full-retention
  window.
