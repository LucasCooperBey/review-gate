"""Tests for review_gate pure logic. Run: python3 test_review_gate.py

No pytest dependency on purpose (this runs in a bare git pre-push hook context)
where only the stdlib python3 is guaranteed. Each test_* function asserts; the
__main__ runner reports PASS/FAIL and exits non-zero on any failure.
"""
import review_gate as rg


# -- classify_files -------------------------------------------------------------

def test_classify_marks_code_vs_docs():
    c = rg.classify_files(["src/util.ts", "README.md", "notes.txt"])
    assert c["has_code"] is True
    assert "src/util.ts" in c["code_files"]
    assert "README.md" in c["doc_files"]
    assert "notes.txt" in c["doc_files"]


def test_classify_docs_only_has_no_code():
    c = rg.classify_files(["docs/plan.md", "memory/MEMORY.md"])
    assert c["has_code"] is False
    assert c["critical"] is False
    assert c["e2e_required"] is False


def test_classify_flags_auth_as_critical():
    c = rg.classify_files(["src/auth/login.ts"])
    assert c["critical"] is True


def test_classify_flags_payments_as_critical():
    c = rg.classify_files(["server/payments/stripe_webhook.py"])
    assert c["critical"] is True


def test_classify_flags_db_migration_as_critical():
    c = rg.classify_files(["db/migrations/0007_add_users.sql"])
    assert c["critical"] is True
    assert c["has_code"] is True


def test_classify_flags_app_routes_as_e2e_required():
    c = rg.classify_files(["app/dashboard/page.tsx"])
    assert c["e2e_required"] is True


def test_classify_plain_lib_is_not_e2e_required():
    c = rg.classify_files(["src/lib/math_helpers.py"])
    assert c["e2e_required"] is False
    assert c["critical"] is False


# -- choose_model ---------------------------------------------------------------

def test_choose_model_defaults_to_sonnet():
    c = rg.classify_files(["src/lib/math_helpers.py"])
    assert rg.choose_model(c) == "sonnet"


def test_choose_model_escalates_critical_to_opus():
    c = rg.classify_files(["src/auth/login.ts"])
    assert rg.choose_model(c) == "opus"


def test_choose_model_override_beats_escalation():
    c = rg.classify_files(["src/auth/login.ts"])
    assert rg.choose_model(c, override="sonnet") == "sonnet"


# -- parse_verdict --------------------------------------------------------------

def test_parse_clean_json():
    v = rg.parse_verdict('{"verdict":"REJECT","bugs":["null deref at L12"],"summary":"bad"}')
    assert v["verdict"] == "REJECT"
    assert v["bugs"] == ["null deref at L12"]
    assert v.get("parse_error") is not True


def test_parse_prose_wrapped_json():
    text = 'Here is my review:\n```json\n{"verdict":"approve","bugs":[],"summary":"clean"}\n```\nThanks.'
    v = rg.parse_verdict(text)
    assert v["verdict"] == "PASS"
    assert v["bugs"] == []


def test_parse_synonyms_map_to_canonical():
    assert rg.parse_verdict('{"verdict":"block"}')["verdict"] == "REJECT"
    assert rg.parse_verdict('{"verdict":"ok"}')["verdict"] == "PASS"
    assert rg.parse_verdict('{"verdict":"warning"}')["verdict"] == "WARN"


def test_parse_takes_last_json_block():
    text = '{"verdict":"PASS"} ... actually reconsidering ... {"verdict":"REJECT","bugs":["x"]}'
    v = rg.parse_verdict(text)
    assert v["verdict"] == "REJECT"


def test_parse_unparseable_is_warn_with_flag():
    v = rg.parse_verdict("I was unable to produce a structured response.")
    assert v["verdict"] == "WARN"
    assert v["parse_error"] is True


def test_parse_unrecognized_verdict_flags_parse_error():
    # a non-canonical verdict value must NOT silently become a clean WARN
    v = rg.parse_verdict('{"verdict":"MAYBE_OK","bugs":[]}')
    assert v["parse_error"] is True


def test_parse_schema_echo_flags_parse_error():
    v = rg.parse_verdict('{"verdict":"PASS|REJECT|WARN"}')
    assert v["parse_error"] is True


def test_parse_real_reject_survives_trailing_schema_echo():
    # the recognized REJECT must win over a later schema echo, not be downgraded
    text = '{"verdict":"REJECT","bugs":["boom"]}\nhere is the schema {"verdict":"PASS|REJECT|WARN"}'
    v = rg.parse_verdict(text)
    assert v["verdict"] == "REJECT"
    assert v.get("parse_error") is not True


def test_parse_normalizes_missing_bugs_to_list():
    v = rg.parse_verdict('{"verdict":"PASS"}')
    assert v["bugs"] == []


# -- exit_code_for ---------------------------------------------------------------

def test_reject_blocks():
    assert rg.exit_code_for({"verdict": "REJECT", "bugs": ["x"]}) == 1


def test_pass_allows():
    assert rg.exit_code_for({"verdict": "PASS", "bugs": []}) == 0


def test_warn_allows():
    assert rg.exit_code_for({"verdict": "WARN", "bugs": []}) == 0


# -- git() binary safety --------------------------------------------------------

def test_git_survives_invalid_utf8_output():
    # Live crash class: a pushed range containing binary blobs makes `git diff`
    # emit invalid utf-8, and a strict text decode kills the gate (fail-open ->
    # push goes unreviewed). git() must replace bad bytes, never raise.
    import subprocess, tempfile, os
    with tempfile.TemporaryDirectory() as d:
        subprocess.run(["git", "-C", d, "init", "-q"], check=True)
        subprocess.run(["git", "-C", d, "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "-q", "--allow-empty", "-m", "base"], check=True)
        with open(os.path.join(d, "blob.bin"), "wb") as f:
            f.write(b"pre\xff\xfe\x80post\n" * 10)
        subprocess.run(["git", "-C", d, "add", "blob.bin"], check=True)
        subprocess.run(["git", "-C", d, "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "-q", "-m", "add binary"], check=True)
        # --text forces the raw bytes into the diff (the crash-shaped output)
        p = rg.git("diff", "--text", "HEAD~1", "HEAD", cwd=d)
        assert p.returncode == 0
        assert "�" in p.stdout  # bad bytes replaced, not raised
        assert "post" in p.stdout    # surrounding content still reviewable


# -- log_verdict (self-contained JSONL spine) ------------------------------------

def test_log_verdict_appends_jsonl_nodes():
    import json, tempfile, os
    with tempfile.TemporaryDirectory() as d:
        nid = rg.log_verdict(d, {"verdict": "WARN", "bugs": ["x"], "summary": "s"},
                             {"head_sha": "a" * 40, "range": "r"})
        assert nid and nid.startswith("review-gate:node:review_verdict:")
        path = os.path.join(d, "evals.jsonl")
        with open(path, encoding="utf-8") as f:
            rows = [json.loads(line) for line in f]
        assert len(rows) == 1
        node = rows[0]
        assert node["type"] == "review_verdict"
        assert node["summary"].startswith("WARN:")
        assert node["payload"]["bugs"] == ["x"]
        assert node["payload"]["range"] == "r"
        assert node["created_at"]
        # append-only: a second verdict adds a second line, never rewrites
        rg.log_verdict(d, {"verdict": "PASS", "bugs": []}, {"head_sha": "b" * 40})
        with open(path, encoding="utf-8") as f:
            rows = [json.loads(line) for line in f]
        assert len(rows) == 2
        assert rows[1]["summary"].startswith("PASS:")


# -- cross-family reviewers -----------------------------------------------------

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile

_MARKER = "ZERO_WHOLE_IS_COMMON"
_GATE_ENV = (
    "REVIEW_GATE_REVIEWER",
    "REVIEW_GATE_REVIEWERS",
    "REVIEW_GATE_AUTHOR",
    "REVIEW_GATE_CODEX_MODEL",
    "REVIEW_GATE_REVIEWER_CMD",
    "REVIEW_GATE_SPINE_DIR",
)
_REAL_WHICH = rg.shutil.which
_REAL_RUN = rg.subprocess.run


def _exe_kind(cmd):
    if isinstance(cmd, str):
        return "cmd"
    if not cmd:
        return ""
    base = os.path.basename(str(cmd[0])).lower()
    if "claude" in base:
        return "claude"
    if "codex" in base:
        return "codex"
    return base


def _verdict_json(verdict, summary):
    bugs = ["div0"] if verdict == "REJECT" else []
    return json.dumps({"verdict": verdict, "bugs": bugs, "summary": summary})


def _write_last(cmd, text):
    if not isinstance(cmd, list) or "--output-last-message" not in cmd:
        return
    path = cmd[cmd.index("--output-last-message") + 1]
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def _init_repo(commits):
    tmp = tempfile.TemporaryDirectory()
    repo = tmp.name
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    for index, files in enumerate(commits, start=1):
        for rel, content in files.items():
            path = os.path.join(repo, rel)
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(content)
            subprocess.run(["git", "add", "--", rel], cwd=repo, check=True)
        subprocess.run(
            ["git", "-c", "user.email=dev@example.com", "-c", "user.name=Dev",
             "commit", "-q", "-m", f"commit {index}"],
            cwd=repo, check=True,
        )
    return tmp, repo


def _code_commits():
    before = "def add(a, b):\n    return a + b\n"
    after = (
        "def add(a, b):\n    return a + b\n\n"
        "def ratio(part, whole):\n"
        "    \"\"\"Return part / whole. ZERO_WHOLE_IS_COMMON for a new account.\"\"\"\n"
        "    return part / whole\n"
    )
    return [{"math_helpers.py": before}, {"math_helpers.py": after}]


def _spine_nodes(spine):
    path = os.path.join(spine, "evals.jsonl")
    with open(path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _run(repo, args, which_map, replies=None, handler=None, env=None):
    args = list(args)
    if "--no-log" not in args and "--spine-dir" not in args:
        args.append("--no-log")
    calls = []

    def default_handler(cmd, **kwargs):
        kind = _exe_kind(cmd)
        verdict, summary = replies[kind]
        body = _verdict_json(verdict, summary)
        if kind == "codex":
            _write_last(cmd, body)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if kind == "claude":
            outer = json.dumps({"type": "result", "result": body})
            return subprocess.CompletedProcess(cmd, 0, stdout=outer, stderr="")
        if kind == "cmd":
            return subprocess.CompletedProcess(cmd, 0, stdout=body, stderr="")
        raise AssertionError(f"unexpected reviewer command: {cmd!r}")

    def wrapped(cmd, *pos, **kwargs):
        if isinstance(cmd, (list, tuple)) and cmd and os.path.basename(str(cmd[0])) == "git":
            return _REAL_RUN(cmd, *pos, **kwargs)
        calls.append((cmd, kwargs))
        if handler is not None:
            return handler(cmd, **kwargs)
        return default_handler(cmd, **kwargs)

    def which(cmd, *pos, **kwargs):
        return which_map.get(cmd)

    old_cwd = os.getcwd()
    old_argv = sys.argv[:]
    saved_env = {key: os.environ.get(key) for key in set(_GATE_ENV) | set(env or {})}
    old_out, old_err = sys.stdout, sys.stderr
    buf_out, buf_err = io.StringIO(), io.StringIO()
    try:
        for key in _GATE_ENV:
            os.environ.pop(key, None)
        if env:
            for key, value in env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        os.chdir(repo)
        sys.argv = ["review_gate.py", *args]
        rg.shutil.which = which
        rg.subprocess.run = wrapped
        sys.stdout, sys.stderr = buf_out, buf_err
        code = rg.main()
        return code, buf_out.getvalue(), buf_err.getvalue(), calls
    finally:
        sys.stdout, sys.stderr = old_out, old_err
        rg.shutil.which = _REAL_WHICH
        rg.subprocess.run = _REAL_RUN
        sys.argv = old_argv
        os.chdir(old_cwd)
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_auto_picks_codex_when_author_is_claude():
    # source: Door 28, 11-12: the public gate could only review with Claude, so a Claude Code user got a same-family review
    tmp, repo = _init_repo(_code_commits())
    try:
        code, out, err, calls = _run(
            repo,
            ["--reviewer", "auto", "--author", "claude", "--range", "HEAD~1..HEAD"],
            {"claude": "/usr/bin/claude", "codex": "/usr/bin/codex"},
            {"claude": ("PASS", "clean"), "codex": ("PASS", "clean")},
        )
        assert code == 0, out
        assert [_exe_kind(cmd) for cmd, _ in calls] == ["codex"]
        cmd, kwargs = calls[0]
        assert "exec" in cmd
        assert "--sandbox" in cmd and cmd[cmd.index("--sandbox") + 1] == "read-only"
        assert "--output-last-message" in cmd
        assert cmd[-1] == "-"
        assert "--model" not in cmd
        assert all(_MARKER not in str(part) for part in cmd)
        assert _MARKER in kwargs.get("input", "")
        assert "PASS" in out
        assert "same-family" not in out
    finally:
        tmp.cleanup()


def test_auto_same_family_when_codex_missing():
    # source: Door 28, 11-12: when no other family is installed, auto uses the author's family and says the review is same-family
    tmp, repo = _init_repo(_code_commits())
    try:
        code, out, err, calls = _run(
            repo,
            ["--reviewer", "auto", "--author", "claude", "--range", "HEAD~1..HEAD"],
            {"claude": "/usr/bin/claude"},
            {"claude": ("PASS", "clean")},
        )
        assert code == 0, out
        assert [_exe_kind(cmd) for cmd, _ in calls] == ["claude"]
        assert "review-gate: same-family review (claude)" in out
    finally:
        tmp.cleanup()


def test_panel_reject_when_one_reviewer_rejects():
    # source: Door 28, 11-12: panel mode is REJECT when any reviewer rejects, including when the first reviewer passed
    tmp, repo = _init_repo(_code_commits())
    try:
        code, out, err, calls = _run(
            repo,
            ["--reviewers", "claude,codex", "--range", "HEAD~1..HEAD"],
            {"claude": "/usr/bin/claude", "codex": "/usr/bin/codex"},
            {"claude": ("PASS", "clean"), "codex": ("REJECT", "zero")},
        )
        assert [_exe_kind(cmd) for cmd, _ in calls] == ["claude", "codex"]
        assert "review-gate [combined] -> REJECT:" in out
        assert code == 1, out
    finally:
        tmp.cleanup()


def test_panel_agreement_false_logged():
    # source: Door 28, 11-12: a panel logs one record per reviewer and a combined record with agreement false when verdicts differ
    tmp, repo = _init_repo(_code_commits())
    spine = tempfile.TemporaryDirectory()
    try:
        code, out, err, calls = _run(
            repo,
            ["--reviewers", "claude,codex", "--spine-dir", spine.name,
             "--range", "HEAD~1..HEAD", "--round", "1"],
            {"claude": "/usr/bin/claude", "codex": "/usr/bin/codex"},
            {"claude": ("PASS", "clean"), "codex": ("REJECT", "zero")},
        )
        assert code == 1, out
        nodes = _spine_nodes(spine.name)
        reviewers = [node["payload"].get("reviewer") for node in nodes]
        assert reviewers.count("claude") == 1
        assert reviewers.count("codex") == 1
        assert reviewers.count("combined") == 1
        combined = next(node for node in nodes if node["payload"].get("panel"))
        assert combined["agreement"] is False
        assert combined["payload"]["agreement"] is False
        assert [member["verdict"] for member in combined["payload"]["members"]] == ["PASS", "REJECT"]
        assert all(node.get("round") == 1 for node in nodes)
        assert all(node["payload"].get("round") == 1 for node in nodes)
    finally:
        spine.cleanup()
        tmp.cleanup()


def test_claude_reviewer_gets_read_only_tools_and_no_mcp():
    # source: Door 28 review, 11-12: the reviewer reads a diff an AI wrote, so text in that diff must not be
    # able to make it edit files or run commands; it keeps read-only tools to look at context
    tmp, repo = _init_repo(_code_commits())
    try:
        code, out, err, calls = _run(
            repo,
            ["--reviewer", "claude", "--range", "HEAD~1..HEAD"],
            {"claude": "/usr/bin/claude"},
            {"claude": ("PASS", "clean")},
        )
        assert code == 0, out
        cmd, kwargs = calls[0]
        assert cmd[cmd.index("--tools") + 1] == "Read,Grep,Glob"
        assert "--strict-mcp-config" in cmd
        assert "--no-session-persistence" in cmd
    finally:
        tmp.cleanup()


def test_claude_prompt_is_stdin_not_argv():
    # source: Door 28, 11-12: the Claude prompt goes on stdin, so the diff is not a command-line argument
    tmp, repo = _init_repo(_code_commits())
    try:
        code, out, err, calls = _run(
            repo,
            ["--reviewer", "claude", "--range", "HEAD~1..HEAD"],
            {"claude": "/usr/bin/claude"},
            {"claude": ("PASS", "clean")},
        )
        assert code == 0, out
        assert len(calls) == 1
        cmd, kwargs = calls[0]
        assert isinstance(cmd, list)
        assert "-p" in cmd
        assert "--model" in cmd
        assert "--output-format" in cmd
        assert "json" in cmd
        assert all(_MARKER not in str(part) for part in cmd)
        assert _MARKER in kwargs.get("input", "")
    finally:
        tmp.cleanup()


def test_cmd_reviewer_runs_env_command():
    # source: Door 28, 11-12: cmd mode runs REVIEW_GATE_REVIEWER_CMD with the prompt on stdin and parses the reply
    tmp, repo = _init_repo(_code_commits())
    try:
        code, out, err, calls = _run(
            repo,
            ["--reviewer", "cmd", "--range", "HEAD~1..HEAD"],
            {},
            handler=lambda cmd, **kwargs: subprocess.CompletedProcess(
                cmd, 0, stdout=_verdict_json("PASS", "clean"), stderr=""),
            env={"REVIEW_GATE_REVIEWER_CMD": "ollama run qwen3:14b"},
        )
        assert code == 0, out
        assert len(calls) == 1
        cmd, kwargs = calls[0]
        assert cmd == "ollama run qwen3:14b"
        assert kwargs.get("shell") is True
        assert kwargs.get("timeout") == 180
        assert _MARKER in kwargs.get("input", "")
        assert "PASS" in out
    finally:
        tmp.cleanup()


def test_auto_says_same_family_when_the_other_family_is_limited():
    # source: Door 28 real run, 11-12: with codex at its usage limit, auto for a Claude author fell back to
    # claude and printed only the limit line, so the person was not told the review was same-family
    tmp, repo = _init_repo(_code_commits())

    def handler(cmd, **kwargs):
        if _exe_kind(cmd) == "codex":
            return subprocess.CompletedProcess(cmd, 0, stdout="You hit your usage limit\n", stderr="")
        outer = json.dumps({"type": "result", "result": _verdict_json("PASS", "clean")})
        return subprocess.CompletedProcess(cmd, 0, stdout=outer, stderr="")

    try:
        code, out, err, calls = _run(
            repo,
            ["--reviewer", "auto", "--author", "claude", "--range", "HEAD~1..HEAD"],
            {"claude": "/usr/bin/claude", "codex": "/usr/bin/codex"},
            handler=handler,
        )
        assert code == 0, out
        assert "review-gate: same-family review (claude)" in out
    finally:
        tmp.cleanup()


def test_auto_usage_limit_on_codex_runs_claude():
    # source: Door 28, 11-12: a usage-limit reply from Codex in auto mode tries the next reviewer and says which one ran
    tmp, repo = _init_repo(_code_commits())

    def handler(cmd, **kwargs):
        kind = _exe_kind(cmd)
        if kind == "codex":
            return subprocess.CompletedProcess(
                cmd, 0, stdout="You hit your usage limit\n", stderr="")
        if kind == "claude":
            body = _verdict_json("PASS", "clean")
            outer = json.dumps({"type": "result", "result": body})
            return subprocess.CompletedProcess(cmd, 0, stdout=outer, stderr="")
        raise AssertionError(f"unexpected reviewer command: {cmd!r}")

    try:
        code, out, err, calls = _run(
            repo,
            ["--reviewer", "auto", "--author", "claude", "--range", "HEAD~1..HEAD"],
            {"claude": "/usr/bin/claude", "codex": "/usr/bin/codex"},
            handler=handler,
        )
        assert [_exe_kind(cmd) for cmd, _ in calls] == ["codex", "claude"]
        assert "review-gate: codex hit usage limit; ran claude" in out
        assert code == 0, out
        assert "PASS" in out
    finally:
        tmp.cleanup()


def test_docs_only_skip_logs_skip_and_exits_0():
    # source: Door 28, 11-12: when classify_files says there is no code, skip the reviewer, log SKIP, list the files, and exit 0
    tmp, repo = _init_repo([
        {"README.md": "alpha\n", "docs/guide.md": "one\n"},
        {"README.md": "beta\n", "docs/guide.md": "two\n"},
    ])
    spine = tempfile.TemporaryDirectory()

    def handler(cmd, **kwargs):
        raise AssertionError(f"reviewer should not run: {cmd!r}")

    try:
        code, out, err, calls = _run(
            repo,
            ["--range", "HEAD~1..HEAD", "--spine-dir", spine.name, "--round", "4"],
            {},
            handler=handler,
        )
        assert code == 0, out
        assert calls == []
        assert "review-gate: docs-only change, skipped." in out
        listed = [line for line in out.splitlines() if "non-source files:" in line]
        assert len(listed) == 1
        assert "README.md" in listed[0]
        assert "docs/guide.md" in listed[0]
        nodes = _spine_nodes(spine.name)
        assert len(nodes) == 1
        assert nodes[0]["payload"]["verdict"] == "SKIP"
        assert nodes[0]["round"] == 4
        assert nodes[0]["payload"]["round"] == 4
    finally:
        spine.cleanup()
        tmp.cleanup()


def test_stats_counts_disagreements():
    # source: Door 28, 11-12: stats counts panels where one family found something the other passed, and prints per-reviewer rates
    tmp, repo = _init_repo(_code_commits())
    spine = tempfile.TemporaryDirectory()
    which = {"claude": "/usr/bin/claude", "codex": "/usr/bin/codex"}
    try:
        code, out, err, calls = _run(
            repo,
            ["--reviewers", "claude,codex", "--spine-dir", spine.name,
             "--range", "HEAD~1..HEAD"],
            which,
            {"claude": ("PASS", "clean"), "codex": ("REJECT", "zero")},
        )
        assert code == 1, out
        code, out, err, calls = _run(
            repo,
            ["--reviewers", "claude,codex", "--spine-dir", spine.name,
             "--range", "HEAD~1..HEAD"],
            which,
            {"claude": ("PASS", "clean"), "codex": ("PASS", "clean")},
        )
        assert code == 0, out
        rg.log_verdict(spine.name, {
            "verdict": "REJECT",
            "bugs": ["x"],
            "summary": "both found issues",
            "reviewer": "combined",
            "panel": True,
            "agreement": False,
            "members": [
                {"reviewer": "claude", "verdict": "REJECT", "infra_error": False},
                {"reviewer": "codex", "verdict": "WARN", "infra_error": False},
            ],
        }, {"head_sha": "c" * 40, "round": 1, "reviewer": "combined", "agreement": False})
        code, out, err, calls = _run(
            repo, ["--stats", "--spine-dir", spine.name], {},
        )
        assert code == 0, err
        assert "claude: reviews 2, REJECT 0, finding rate 0%" in out
        assert "codex: reviews 2, REJECT 1, finding rate 50%" in out
        assert "panel disagreements: 1" in out
        assert "reviews logged:" in out
    finally:
        spine.cleanup()
        tmp.cleanup()


def test_a_reject_that_mentions_limits_still_blocks():
    # source: 11-12 pre-push review of this repo: limit words anywhere in the reply turned a real REJECT
    # into a fail-open WARN, so a review of rate-limit code could never block
    tmp, repo = _init_repo(_code_commits())
    body = json.dumps({"verdict": "REJECT", "bugs": ["the 429 retry loop never stops"],
                       "summary": "rate limit handler: try again path loops; quota ignored"})

    def handler(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd, 0, stdout=json.dumps({"type": "result", "result": body}), stderr="")

    try:
        code, out, err, calls = _run(
            repo, ["--reviewer", "claude", "--range", "HEAD~1..HEAD"],
            {"claude": "/usr/bin/claude"}, handler=handler,
        )
        assert code == 1, out
        assert "fail-open" not in out, out
    finally:
        tmp.cleanup()


def test_codex_stderr_echo_cannot_downgrade_a_reject():
    # source: 11-12 pre-push review of this repo: Codex can echo the prompt, and so the diff, on stderr;
    # a diff comment saying "usage limit" made a REJECT read as a limit
    tmp, repo = _init_repo(_code_commits())

    def handler(cmd, **kwargs):
        _write_last(cmd, _verdict_json("REJECT", "zero"))
        return subprocess.CompletedProcess(
            cmd, 0, stdout="", stderr="user\n# handles the usage limit, see @@ -429,6 @@\n")

    try:
        code, out, err, calls = _run(
            repo, ["--reviewer", "codex", "--range", "HEAD~1..HEAD"],
            {"codex": "/usr/bin/codex"}, handler=handler,
        )
        assert code == 1, out
    finally:
        tmp.cleanup()


def test_reviewer_keys_cannot_mark_a_verdict_as_infra():
    # source: 11-12 security review of this repo: parse_verdict kept every key of the reviewer's JSON, so a
    # reply carrying infra_error and limit_marker was dropped by auto and the next reviewer's PASS shipped
    tmp, repo = _init_repo(_code_commits())
    spoof = json.dumps({"verdict": "REJECT", "bugs": ["real bug"], "summary": "bad",
                        "infra_error": True, "limit_marker": "quota"})

    def handler(cmd, **kwargs):
        kind = _exe_kind(cmd)
        if kind == "claude":
            return subprocess.CompletedProcess(
                cmd, 0, stdout=json.dumps({"type": "result", "result": spoof}), stderr="")
        if kind == "codex":
            _write_last(cmd, _verdict_json("PASS", "clean"))
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        raise AssertionError(f"unexpected reviewer command: {cmd!r}")

    try:
        code, out, err, calls = _run(
            repo, ["--reviewer", "auto", "--author", "codex", "--range", "HEAD~1..HEAD"],
            {"claude": "/usr/bin/claude", "codex": "/usr/bin/codex"}, handler=handler,
        )
        assert code == 1, out
        assert [_exe_kind(cmd) for cmd, _ in calls] == ["claude"], calls
    finally:
        tmp.cleanup()


def test_parse_verdict_keeps_only_review_keys():
    # source: 11-12 security review of this repo: bookkeeping keys must come from the gate, never the reviewer
    out = rg.parse_verdict(json.dumps({
        "verdict": "PASS", "bugs": [], "summary": "ok", "e2e_present": True,
        "infra_error": True, "limit_marker": "quota", "panel": True, "members": [],
        "agreement": True, "reviewer": "someone", "model": "x", "round": 9,
    }))
    assert out["verdict"] == "PASS"
    assert out["e2e_present"] is True
    for key in ("infra_error", "limit_marker", "panel", "members", "agreement", "reviewer", "model", "round"):
        assert key not in out, key


def test_a_reviewer_that_cannot_start_is_an_infra_warn():
    # source: 11-12 pre-push review of this repo: a reviewer binary without execute permission raised
    # PermissionError, which crashed the hook with a traceback instead of failing open
    tmp, repo = _init_repo(_code_commits())

    def handler(cmd, **kwargs):
        raise PermissionError(13, "Permission denied", str(cmd[0]))

    try:
        code, out, err, calls = _run(
            repo, ["--reviewer", "claude", "--range", "HEAD~1..HEAD"],
            {"claude": "/usr/bin/claude"}, handler=handler,
        )
        assert code == 0, out
        assert "could not start" in out, out
    finally:
        tmp.cleanup()


def test_auto_names_the_limit_when_no_reviewer_is_left():
    # source: 11-12 pre-push review of this repo: with codex at its limit and no claude installed, auto
    # returned the limit WARN without saying which reviewer hit it
    tmp, repo = _init_repo(_code_commits())
    home = tempfile.TemporaryDirectory()

    def handler(cmd, **kwargs):
        if _exe_kind(cmd) == "codex":
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="You've hit your usage limit\n")
        raise AssertionError(f"unexpected reviewer command: {cmd!r}")

    try:
        code, out, err, calls = _run(
            repo, ["--reviewer", "auto", "--author", "claude", "--range", "HEAD~1..HEAD"],
            {"codex": "/usr/bin/codex"}, handler=handler, env={"HOME": home.name},
        )
        assert code == 0, out
        assert "review-gate: codex hit usage limit; no other reviewer is available" in out, out
    finally:
        home.cleanup()
        tmp.cleanup()


def test_stats_leave_out_skips_and_combined_records():
    # source: 11-12 pre-push review of this repo: SKIP rows and the panel's combined row were scored as
    # reviews, so a docs-only push lowered the finding rate and a panel counted twice
    tmp, repo = _init_repo(_code_commits())
    spine = tempfile.TemporaryDirectory()
    try:
        meta = {"head_sha": "d" * 40, "round": 1}
        rg.log_verdict(spine.name, {"verdict": "SKIP", "bugs": [], "summary": "docs"}, meta)
        rg.log_verdict(spine.name, {"verdict": "PASS", "bugs": [], "summary": "ok", "reviewer": "claude"}, meta)
        rg.log_verdict(spine.name, {"verdict": "REJECT", "bugs": ["x"], "summary": "bad", "reviewer": "codex"}, meta)
        rg.log_verdict(spine.name, {"verdict": "REJECT", "bugs": ["x"], "summary": "claude PASS; codex REJECT",
                                    "reviewer": "combined", "panel": True, "agreement": False,
                                    "members": []}, meta)
        code, out, err, calls = _run(repo, ["--stats", "--spine-dir", spine.name], {})
        assert code == 0, err
        assert "scored (non-infra):    2" in out, out
        assert "carried >=1 finding:   1" in out, out
        assert "skipped (docs-only):   1" in out, out
    finally:
        spine.cleanup()
        tmp.cleanup()


def test_auto_tries_the_next_reviewer_after_a_timeout():
    # source: 11-12 pre-push review of this repo: auto moved on only after a usage limit, so a timed-out or
    # logged-out cross-family reviewer let the push through unreviewed while claude was installed
    tmp, repo = _init_repo(_code_commits())

    def handler(cmd, **kwargs):
        kind = _exe_kind(cmd)
        if kind == "codex":
            raise subprocess.TimeoutExpired(cmd, 180)
        if kind == "claude":
            body = _verdict_json("REJECT", "zero")
            return subprocess.CompletedProcess(
                cmd, 0, stdout=json.dumps({"type": "result", "result": body}), stderr="")
        raise AssertionError(f"unexpected reviewer command: {cmd!r}")

    try:
        code, out, err, calls = _run(
            repo, ["--reviewer", "auto", "--author", "claude", "--range", "HEAD~1..HEAD"],
            {"claude": "/usr/bin/claude", "codex": "/usr/bin/codex"}, handler=handler,
        )
        assert [_exe_kind(cmd) for cmd, _ in calls] == ["codex", "claude"], calls
        assert "review-gate: codex failed (reviewer timed out after 180s); ran claude" in out, out
        assert code == 1, out
    finally:
        tmp.cleanup()


def test_an_unknown_author_value_is_named():
    # source: 11-12 pre-push review of this repo: REVIEW_GATE_AUTHOR="Claude Code" was silently read as unset,
    # so a Claude-written change got a Claude review with no notice
    tmp, repo = _init_repo(_code_commits())
    try:
        code, out, err, calls = _run(
            repo, ["--reviewer", "auto", "--range", "HEAD~1..HEAD"],
            {"claude": "/usr/bin/claude"}, {"claude": ("PASS", "clean")},
            env={"REVIEW_GATE_AUTHOR": "Claude Code"},
        )
        assert code == 0, out
        assert "REVIEW_GATE_AUTHOR='Claude Code' is not claude, codex or other" in out, out
    finally:
        tmp.cleanup()


def test_run_helper_restores_every_variable_it_sets():
    # source: 11-12 pre-push review of this repo: _run set HOME for one test and never put it back
    before = os.environ.get("HOME")
    tmp, repo = _init_repo(_code_commits())
    try:
        _run(repo, ["--reviewer", "claude", "--range", "HEAD~1..HEAD"],
             {"claude": "/usr/bin/claude"}, {"claude": ("PASS", "clean")},
             env={"HOME": "/nonexistent-review-gate-home"})
        assert os.environ.get("HOME") == before
    finally:
        tmp.cleanup()


if __name__ == "__main__":
    import sys
    fns = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in fns:
        try:
            fn()
            print(f"PASS: {name}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL: {name} -- {e!r}")
        except Exception as e:  # noqa: BLE001: surface import/logic errors as failures
            failed += 1
            print(f"ERROR: {name} -- {type(e).__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
