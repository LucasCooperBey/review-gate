<h1 align="center">review-gate</h1>

<p align="center"><strong>A fresh-context code review on every git push, from the other model family if you want it. REJECT blocks the push.</strong></p>

<p align="center">
  <a href="https://github.com/LucasCooperBey/100-doors"><img alt="100 Doors: Doors 13 and 28" src="https://img.shields.io/badge/100%20Doors-Doors%2013%20and%2028-f94bf3"></a>
  <a href="LICENSE"><img alt="license MIT" src="https://img.shields.io/badge/license-MIT-9aff4a"></a>
  <img alt="python 3.10+" src="https://img.shields.io/badge/python-3.10%2B-3776ab">
  <img alt="dependencies none" src="https://img.shields.io/badge/dependencies-none-9aff4a">
  <img alt="reviewers Claude · Codex · any CLI" src="https://img.shields.io/badge/reviewers-Claude%20%C2%B7%20Codex%20%C2%B7%20any%20CLI-5ac4bf">
  <a href="https://github.com/LucasCooperBey/review-gate/commits/main"><img alt="last commit" src="https://img.shields.io/github/last-commit/LucasCooperBey/review-gate?color=9aff4a&label=updated"></a>
</p>

<p align="center"><a href="#install">Install</a> · <a href="#example">Example</a> · <a href="#cross-family-review">Cross-family review</a> · <a href="#tests">Tests</a> · <a href="https://github.com/LucasCooperBey/100-doors">All 100 Doors</a></p>

Fresh-context adversarial code review on every `git push`. The reviewer can be the Claude Code CLI, the Codex CLI, both as a panel, or any command that reads a prompt on stdin.

Each push is reviewed by a separate process that sees only the diff, the changed-file list, and (optionally) an acceptance spec. It does not see the conversation or editor session that wrote the code. A structured verdict comes back: **REJECT blocks the push** (exit 1), PASS and WARN let it through with the findings printed. Every verdict is appended to a local JSONL log so you can measure how often fresh eyes catch something.

**Who it's for:** solo builders and small teams who let an AI agent write code and want a second opinion that did not write it before the code lands.

## Contents

- [What's inside](#whats-inside)
- [Why fresh context](#why-fresh-context)
- [Requirements](#requirements)
- [Install](#install)
- [Usage](#usage)
- [Example](#example)
- [Cross-family review](#cross-family-review)
- [Verdict rules given to the reviewer](#verdict-rules-given-to-the-reviewer)
- [When the reviewer cannot answer](#when-the-reviewer-cannot-answer)
- [The spine log](#the-spine-log)
- [Tests](#tests)
- [Related](#related)
- [Part of 100 Doors](#part-of-100-doors)
- [Credits](#credits)
- [License](#license)

## What's inside

| Path | What it is |
|---|---|
| `review_gate.py` | The gate: one Python file, standard library only |
| `install.sh` | Writes a thin pre-push hook into the current repo |
| `docs/cross-family-loop.md` | The by-hand loop: review, fix, re-review across model families |
| `test_review_gate.py` | Hermetic tests, no network: `python3 test_review_gate.py` |
| `spine/` | Where every verdict is appended as JSONL |
| `CHANGELOG.md` | What changed and when |

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

Flags: `--model auto|sonnet|opus` (auto = Sonnet, escalating to Opus when the diff touches auth, payment, or migration paths), `--reviewer auto|claude|codex|cmd`, `--author claude|codex|other`, `--reviewers claude,codex`, `--codex-model <name>`, `--round N`, `--timeout <seconds>` (default scales from 180 to 300 with the diff; an explicit value is clamped to 600), `--fail-open-always`, `--spine-dir <dir>` (or `$REVIEW_GATE_SPINE_DIR`), `--no-log`.

`--model` chooses Sonnet or Opus for the Claude reviewer. `--codex-model` (or `$REVIEW_GATE_CODEX_MODEL`) chooses the Codex model. When it is unset, the Codex command gets no model flag.

## Example

A change marked as written by Codex, which claims to handle an empty list:

```python
def average(xs):
    """Mean of xs; returns 0 for an empty list."""
    total = 0
    for i in range(1, len(xs)):
        total += xs[i]
    return total / len(xs)
```

`REVIEW_GATE_AUTHOR=codex git push` sends it to the Claude reviewer, and the push stops (this run was recorded on 2026-11-12):

```text
review-gate [claude/sonnet] -> REJECT: The loop skips the first element, so every non-empty average is wrong, and the empty-list case the docstring claims to handle still raises ZeroDivisionError.
  - [high] stats.py:4 The loop uses range(1, len(xs)), so it skips xs[0]. ...
  - [high] stats.py:6 The docstring promises 0 for an empty list, but nothing guards that case. ...
  - [low] stats.py:3 The hand-rolled loop replaces sum(xs), which is simpler and correct. ...
  - [med] stats.py:1 The diff adds no test. ...

Push BLOCKED by review-gate. Fix the findings, or override (logged): git push --no-verify
```

The findings are shortened here. A reviewer can still miss a bug or report one that is not there, so read each finding before you act on it.

## Cross-family review

Cross-family review means the reviewer is a different model family from the author of the change. You run that review by hand: review, fix or answer each finding, and rerun until the verdict is clean or you stop after three rounds.

`--reviewer auto` is the default. It reads `--author` or `$REVIEW_GATE_AUTHOR` and picks the first installed reviewer from another family:

- author `claude`: Codex, then the `cmd` reviewer
- author `codex`: Claude, then the `cmd` reviewer
- author `other`, or author unset: Claude, then Codex, then the `cmd` reviewer

Claude is installed when `claude` is on PATH (or at `~/.local/bin/claude`). Codex is installed when `codex` is on PATH. The `cmd` reviewer is installed when `REVIEW_GATE_REVIEWER_CMD` is set. If only the author's own family is installed, auto uses it and prints `review-gate: same-family review (<family>)`. If the chosen reviewer gives no verdict because it hit a usage limit, timed out, or could not start, auto tries the next reviewer and prints which one ran. A limit is read from a usage limit, rate limit, quota, 429, or try-again message in output that holds no verdict. A reply that carries a verdict always stands, so a REJECT that mentions a rate limit still blocks, and words in the diff cannot turn a review into a limit.

`--reviewers claude,codex` (or `$REVIEW_GATE_REVIEWERS`) is panel mode. Each listed reviewer sees the same prompt. The combined verdict is REJECT if any reviewer returns REJECT, WARN if any returns WARN or an infra error, and PASS otherwise. The gate prints a verdict line per reviewer and a combined line. The spine gets one record per reviewer plus one combined record with `agreement` (true when every verdict is the same, false otherwise). A `--reviewer` flag selects one reviewer and overrides `$REVIEW_GATE_REVIEWERS`. Panel reviewers run one after another and share one 900 second wall, including timeout retries.

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

## When the reviewer cannot answer

A timeout is retried once, at 2.5 times that attempt's budget, before that reviewer counts as failed. The default budget is 180 seconds plus one second per 500 diff bytes, clamped to 180 to 300 seconds. Pass `--timeout` to set the budget; that value is clamped to 600 seconds for one attempt. Every attempt and every reviewer share a 900 second wall, and a retry is skipped when under 30 seconds remain, so the retry cannot turn a push into a long hang.

On a critical path (auth, payment, migration, or secrets), no usable verdict after every attempt and every reviewer blocks the push with exit 3, and the log records `BLOCKED_NO_VERDICT`. Exit 1 still means the reviewer returned REJECT. On every other change, a missing reviewer, a timeout, a bad exit, unparseable output, or a crash of the gate still allows the push with a loud WARN.

```sh
REVIEW_GATE_FAIL_OPEN_ALWAYS=1 git push   # --fail-open-always: allow this gate even with no verdict
REVIEW_GATE_SKIP=1 git push               # skip just the review
git push --no-verify                      # skip all pre-push hooks
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

## Part of 100 Doors

This tool is the gift behind two doors of [100 Doors](https://github.com/LucasCooperBey/100-doors): **Door 13** (fresh eyes on every push) and **Door 28** (review with another model family).

- Door 13 post: [X](https://x.com/i/status/2114228769783447769) · [Skool](https://www.skool.com/sandiegoaistudio/door-13-review-gate)
- Door 28 and every other door: [the 100 Doors index](https://github.com/LucasCooperBey/100-doors)
- Related doors: [harness-guard](https://github.com/LucasCooperBey/harness-guard) (Doors 12 and 30), [shipgate](https://github.com/LucasCooperBey/shipgate) (Door 14), [pp2p and pp3p](https://github.com/LucasCooperBey/claude-skills/tree/main/skills/pp2p) (Door 7)
- The season: [sandiegoaistudio.com/season](https://sandiegoaistudio.com/season)

### Come see it live

If you want to see how this and the rest of the doors open up into my software factory, come to [Sharathon](https://sandiegoaistudio.com/sharathon), San Diego AI Studio's show-your-setup day. Sharathon 3 is Sunday, Nov 15 in La Mesa: [RSVP on Luma](https://luma.com/5y5ju7ud).

## Credits

- Reviews run on the [Claude Code CLI](https://claude.com/claude-code), the Codex CLI, or any command you name
- Built by [Lucas Cooper-Bey](https://github.com/LucasCooperBey)

## License

MIT
