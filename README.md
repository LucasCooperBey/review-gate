# review-gate

Fresh-context adversarial code review on every `git push`. The reviewer can be the Claude Code CLI, the Codex CLI, both as a panel, or any command that reads a prompt on stdin.

Each push is reviewed by a separate process that sees only the diff, the changed-file list, and (optionally) an acceptance spec. It does not see the conversation or editor session that wrote the code. A structured verdict comes back: **REJECT blocks the push** (exit 1), PASS and WARN let it through with the findings printed. Every verdict is appended to a local JSONL log so you can measure how often fresh eyes catch something.

## Why fresh context

Asking the same model (or the same human) that wrote the code "any bugs?" returns the author's blind spots. The author's context, the plan, the assumptions, and the "I already handled that" is the bias. A reviewer process with none of that context has to judge the diff on what it actually says.

In real use, the first thing this gate did after being wired up was review its own installation commits. It reported findings across three consecutive rounds (an unsubstantiated "tested" claim, a failure path that reported success, a spoofable substring match, and a crash-window race reproduced by hand) before the code reached PASS.

## Requirements

- `git` and `python3` 3.10+ (stdlib only, no packages)
- A reviewer on the machine: the [Claude Code CLI](https://claude.com/claude-code) (`claude` on PATH), the Codex CLI (`codex` on PATH), or any command that reads the prompt on stdin

## Install

```sh
git clone https://github.com/LucasCooperBey/review-gate
cd your-project
/path/to/review-gate/install.sh
```

`install.sh` writes a thin `pre-push` hook into the current repo (it refuses to overwrite an existing hook; chain it manually in that case). To gate every repo on a machine, point `core.hooksPath` at a dispatcher directory and call `review_gate.py --pre-push` from there.

The hook inherits `REVIEW_GATE_REVIEWER`, `REVIEW_GATE_AUTHOR`, `REVIEW_GATE_REVIEWERS`, and `REVIEW_GATE_REVIEWER_CMD` from the shell that runs `git push`.

## Usage

```sh
# As a hook: nothing to do. Push and the review runs.
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

Flags: `--model auto|sonnet|opus` (auto = Sonnet, escalating to Opus when the diff touches auth, payment, or migration paths), `--reviewer auto|claude|codex|cmd`, `--author claude|codex|other`, `--reviewers claude,codex`, `--codex-model <name>`, `--round N`, `--timeout 180`, `--spine-dir <dir>` (or `$REVIEW_GATE_SPINE_DIR`), `--no-log`.

`--model` chooses Sonnet or Opus for the Claude reviewer. `--codex-model` (or `$REVIEW_GATE_CODEX_MODEL`) chooses the Codex model. When it is unset, the Codex command gets no model flag.

## Cross-family review

Cross-family review means the reviewer is a different model family from the author of the change. You run that review by hand: review, fix or answer each finding, and rerun until the verdict is clean or you stop after three rounds.

`--reviewer auto` is the default. It reads `--author` or `$REVIEW_GATE_AUTHOR` and picks the first installed reviewer from another family:

- author `claude`: Codex, then the `cmd` reviewer
- author `codex`: Claude, then the `cmd` reviewer
- author `other`, or author unset: Claude, then Codex, then the `cmd` reviewer

Claude is installed when `claude` is on PATH (or at `~/.local/bin/claude`). Codex is installed when `codex` is on PATH. The `cmd` reviewer is installed when `REVIEW_GATE_REVIEWER_CMD` is set. If only the author's own family is installed, auto uses it and prints `review-gate: same-family review (<family>)`. If the chosen reviewer gives no verdict and its output reports a usage limit, rate limit, quota, 429, or says to try again, auto tries the next reviewer and prints which one ran. A reply that carries a verdict always stands, so a REJECT that mentions a rate limit still blocks, and words in the diff cannot turn a review into a limit.

`--reviewers claude,codex` (or `$REVIEW_GATE_REVIEWERS`) is panel mode. Each listed reviewer sees the same prompt. The combined verdict is REJECT if any reviewer returns REJECT, WARN if any returns WARN or an infra error, and PASS otherwise. The gate prints a verdict line per reviewer and a combined line. The spine gets one record per reviewer plus one combined record with `agreement` (true when every verdict is the same, false otherwise). A `--reviewer` flag selects one reviewer and overrides `$REVIEW_GATE_REVIEWERS`. Panel reviewers run one after another, so the longest wait is `--timeout` times the number of reviewers.

`--reviewer cmd` runs `$REVIEW_GATE_REVIEWER_CMD` in the shell. The command reads the prompt on stdin and prints the reply:

```sh
REVIEW_GATE_REVIEWER_CMD='ollama run qwen3:14b' python3 review_gate.py --reviewer cmd
REVIEW_GATE_REVIEWER_CMD='gemini -p' python3 review_gate.py --reviewer cmd
```

The hand loop, including `--round`, is in [docs/cross-family-loop.md](docs/cross-family-loop.md).

## Verdict rules given to the reviewer

- **REJECT**: any high-severity correctness or security bug (data loss, auth bypass, injection, hot-path null deref, race, money or units error).
- **WARN**: only medium or low concerns, or an app-facing change with no E2E evidence in the diff (unit tests alone do not count).
- **PASS**: nothing actionable found.

Docs-only changes skip the reviewer, print `review-gate: docs-only change, skipped.`, list the non-source files on one line, log a SKIP record, and exit 0. A `.git/review-gate-pass` sentinel remembers the last reviewed tip so a clean re-push does not re-run the review.

## Fail-open, on purpose

If the reviewer is missing, times out, or errors, and if the gate itself crashes, the push is **allowed** with a loud WARN. Exit 1 always means "reviewed and REJECTed", never "the tooling broke". The gate is additive safety, not the only safety. Robustness details that came from real incidents live in the code comments (for example binary-safe diff decoding, and schema-echo-proof verdict parsing).

Escape hatches (both intentional, both visible in your shell history):

```sh
REVIEW_GATE_SKIP=1 git push   # skip just the review
git push --no-verify          # skip all pre-push hooks
```

## The spine log

Verdicts append to `spine/evals.jsonl` as typed, timestamped nodes (the envelope follows the [spine pattern](https://github.com/LucasCooperBey/spine-pattern)). Each record stores `--round` (default 1). `--stats` reads the log back:

```
review-gate stats (.../spine)
  reviews logged:        31
  scored (non-infra):    29
  carried >=1 finding:   19
  pre-gate finding rate: 66%   <- how often fresh eyes caught something
  claude: reviews 20, REJECT 8, finding rate 55%
  codex: reviews 11, REJECT 4, finding rate 45%
  panel disagreements: 3
```

The per-reviewer lines count that reviewer's reviews, REJECT count, and finding rate. When panel records exist, `panel disagreements` is the number of panels where one family found something (REJECT or WARN) and another family passed.

## Tests

```sh
python3 test_review_gate.py   # stdlib-only, hermetic, no network
```

## Related

- [treehouse](https://github.com/LucasCooperBey/treehouse): git worktree pool manager for running parallel work the gate reviews
- [harness-guard](https://github.com/LucasCooperBey/harness-guard): drop-in safety hooks for agent sessions
- [shipgate](https://github.com/LucasCooperBey/shipgate): fail-closed secrets and licensing gate for publishing

## License

MIT

Made by Lucas Cooper-Bey for 100 Doors, one free tool a day: https://sandiegoaistudio.com/season
If you like seeing how other builders review their agents' code, San Diego AI Studio runs Sharathon, where people show their own setups: https://www.skool.com/sandiegoaistudio
