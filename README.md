# review-gate

Fresh-context adversarial code review on every `git push`, powered by the Claude Code CLI.

Each push is reviewed by a **separate** `claude -p` process that sees only the diff, the changed-file list, and (optionally) an acceptance spec — never the conversation or editor session that wrote the code. A structured verdict comes back: **REJECT blocks the push** (exit 1), PASS/WARN let it through with the findings printed. Every verdict is appended to a local JSONL log so you can measure how often fresh eyes catch something.

## Why fresh context

Asking the same model (or the same human) that wrote the code "any bugs?" returns the author's blind spots. The author's context — the plan, the assumptions, the "I already handled that" — is exactly the bias. A reviewer process with none of that context has to judge the diff on what it actually says.

In real use, the first thing this gate did after being wired up was review its own installation commits — and it produced genuine findings across three consecutive rounds (an unsubstantiated "tested" claim, a failure path that reported success, a spoofable substring match, and a crash-window race it reproduced by hand) before the code converged to PASS. The tool's own development history is its best demo.

## Requirements

- `git`, `python3` 3.10+ (stdlib only, no packages)
- [Claude Code CLI](https://claude.com/claude-code) installed and authenticated (`claude` on PATH)

## Install

```sh
git clone https://github.com/Lucface/review-gate
cd your-project
/path/to/review-gate/install.sh
```

`install.sh` writes a thin `pre-push` hook into the current repo (it refuses to overwrite an existing hook — chain it manually in that case). To gate **every** repo on a machine, point `core.hooksPath` at a dispatcher directory and call `review_gate.py --pre-push` from there.

## Usage

```sh
# As a hook: nothing to do — push and the review runs.
git push

# Ad-hoc review of the working tree vs HEAD (default mode)
python3 review_gate.py

# Staged changes only
python3 review_gate.py --staged

# An explicit range
python3 review_gate.py --range origin/main..HEAD

# Check the diff against an acceptance spec too
python3 review_gate.py --range origin/main..HEAD --spec docs/spec.md

# How often has the gate caught something?
python3 review_gate.py --stats
```

Flags: `--model auto|sonnet|opus` (auto = Sonnet, escalating to Opus when the diff touches auth/payment/migration paths), `--timeout 180`, `--spine-dir <dir>` (or `$REVIEW_GATE_SPINE_DIR`), `--no-log`.

## Verdict rules given to the reviewer

- **REJECT** — any high-severity correctness or security bug (data loss, auth bypass, injection, hot-path null deref, race, money/units error).
- **WARN** — only medium/low concerns, or an app-facing change with no E2E evidence in the diff (unit tests alone don't count).
- **PASS** — nothing actionable found.

Docs-only pushes are skipped. A `.git/review-gate-pass` sentinel remembers the last reviewed tip so a clean re-push doesn't re-run the review.

## Fail-open, on purpose

If `claude` is missing, times out, or errors — and if the gate itself crashes — the push is **allowed** with a loud WARN. Exit 1 always means "reviewed and REJECTed", never "the tooling broke". The gate is additive safety, not the only safety. Robustness details that came from real incidents live in the code comments (e.g. binary-safe diff decoding, schema-echo-proof verdict parsing).

Escape hatches (both intentional, both visible in your shell history):

```sh
REVIEW_GATE_SKIP=1 git push   # skip just the review
git push --no-verify          # skip all pre-push hooks
```

## The spine log

Verdicts append to `spine/evals.jsonl` as typed, timestamped nodes (the envelope follows the [spine pattern](https://github.com/Lucface/spine-pattern)). `--stats` reads it back:

```
review-gate stats (.../spine)
  reviews logged:        31
  scored (non-infra):    29
  carried >=1 finding:   19
  pre-gate finding rate: 66%   <- how often fresh eyes caught something
```

## Tests

```sh
python3 test_review_gate.py   # stdlib-only, hermetic, no network
```

## Related

- [treehouse](https://github.com/Lucface/treehouse) — git worktree pool manager for running parallel work the gate reviews
- [harness-guard](https://github.com/Lucface/harness-guard) — drop-in safety hooks for agent sessions
- [shipgate](https://github.com/Lucface/shipgate) — fail-closed secrets/licensing gate for publishing

## License

MIT
