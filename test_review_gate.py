"""Tests for review_gate pure logic. Run: python3 test_review_gate.py

No pytest dependency on purpose — this runs in a bare git pre-push hook context
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
        except Exception as e:  # noqa: BLE001 — surface import/logic errors as failures
            failed += 1
            print(f"ERROR: {name} -- {type(e).__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
