#!/usr/bin/env python3
"""Read-only fixture/hash inventory for externally managed Temporal adapters.

This never imports adapters, inspects credentials, contacts services or deploys.
Hash equality is snapshot evidence, not runtime or human-approval evidence.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = "scripts/automation-rollout.json"
FIXTURE = "scripts/fixtures/automation-formatting.json"
PLAN = ".opencode/skills/planning/references/create-plan.json"
COMPONENTS = {
    "docs": "successor/docs.py",
    "attention": "successor/attention.py",
    "github": "successor/github.py",
    "service": "successor/service.py",
}
SOURCES = {
    ".opencode/skills/planning/scripts/build_plan.py",
    ".opencode/skills/planning/references/plan-content.json",
    PLAN,
    ".opencode/skills/planning/tests/test_build_plan.py",
    ".opencode/skills/paseo-telegram-attention/scripts/attention_prompt.py",
    ".opencode/skills/paseo-telegram-attention/scripts/telegram_attention.py",
    ".opencode/skills/paseo-telegram-attention/scripts/attention_receiver.py",
    ".opencode/skills/paseo-telegram-attention/scripts/test_attention_prompt.py",
    ".opencode/skills/paseo-telegram-attention/scripts/test_telegram_attention.py",
    ".opencode/skills/paseo-telegram-attention/scripts/test_attention_receiver.py",
    ".opencode/skills/paseo-telegram-attention/scripts/test_callback_receiver.py",
    "scripts/tests/test_classifier_skill.py",
    "scripts/tests/test_issue_to_pr_skill.py",
    FIXTURE,
}
LIMIT = 2 * 1024 * 1024
SHA256 = re.compile(r"[0-9a-f]{64}")
LIMITATIONS = [
    "Repository fixtures and explicit snapshot hashes only; no external code is executed.",
    "Hashes do not establish installed identity, callback verification, App authorship or receiver ownership.",
    "Live Docs/Telegram/GitHub, desktop/mobile visual acceptance and Caddy checks are not run.",
    "Human review and native approval gates remain required; this report grants no authority.",
]


class ContractError(ValueError):
    """Diagnostics never include input values, exception text or file contents."""


def require(condition):
    if not condition:
        raise ContractError("contract_mismatch")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_regular(root, relative):
    """Bound reads to regular allowlisted files, refusing symlinks and special files."""
    parts = relative.split("/")
    if not parts or any(part in {"", ".", ".."} for part in parts) or relative.startswith("/"):
        raise ContractError("unsafe_path")
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        with os.fdopen(file_fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > LIMIT:
                raise ContractError("unsafe_file")
            data = stream.read(LIMIT + 1)
            if len(data) > LIMIT:
                raise ContractError("oversized_file")
            return data
    finally:
        os.close(fd)


def json_bytes(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ContractError("duplicate_key")
            result[key] = value
        return result

    try:
        return json.loads(data, object_pairs_hook=unique,
                          parse_constant=lambda _: require(False))
    except (ValueError, UnicodeError, RecursionError):
        raise ContractError("invalid_json") from None


def validate_manifest(value):
    require(isinstance(value, dict) and set(value) == {
        "schema_version", "runtime", "source_sha256", "components"})
    require(type(value["schema_version"]) is int and value["schema_version"] == 1)
    require(value["runtime"] == "temporal")
    hashes = value["source_sha256"]
    require(isinstance(hashes, dict) and set(hashes) == SOURCES)
    require(all(isinstance(h, str) and SHA256.fullmatch(h) for h in hashes.values()))
    components = value["components"]
    require(isinstance(components, dict) and set(components) == set(COMPONENTS))
    for name, path in COMPONENTS.items():
        component = components[name]
        require(isinstance(component, dict) and set(component) == {"path", "contracts", "references"})
        require(component["path"] == path)
        require(isinstance(component["contracts"], list) and component["contracts"])
        require(all(isinstance(c, str) and re.fullmatch(r"[a-z][a-z0-9_]+", c)
                    for c in component["contracts"]))
        require(isinstance(component["references"], list) and component["references"])
        require(all(isinstance(p, str) and p in SOURCES for p in component["references"]))
    return value


def validate_snapshot_lock(value):
    require(isinstance(value, dict) and set(value) == {"schema_version", "runtime", "source_sha256"})
    require(type(value["schema_version"]) is int and value["schema_version"] == 1)
    require(value["runtime"] == "temporal")
    hashes = value["source_sha256"]
    require(isinstance(hashes, dict) and set(hashes) == set(COMPONENTS.values()))
    require(all(isinstance(h, str) and SHA256.fullmatch(h) for h in hashes.values()))
    return hashes


def check_docs(batch):
    """Check the creation fixture only, never apply it to an existing document."""
    require(isinstance(batch, dict) and set(batch) == {"requests"})
    requests = batch["requests"]
    require(isinstance(requests, list) and len(requests) >= 8)
    require(all(isinstance(r, dict) and len(r) == 1 for r in requests))
    require(requests[0]["insertText"]["location"] == {"index": 1})
    text = requests[0]["insertText"]["text"]
    require(isinstance(text, str) and text.startswith("Status: \n\n") and "\t" not in text)
    encoded = text.encode("utf-16-le")
    kinds, presets, links, code = set(), set(), [], []
    for request in requests[1:-2]:
        name, op = next(iter(request.items()))
        require(name in {"updateTextStyle", "updateParagraphStyle", "createParagraphBullets"})
        interval = op["range"]
        left, right = interval["startIndex"], interval["endIndex"]
        require(type(left) is int and type(right) is int and 11 <= left < right <= 1 + len(encoded) // 2)
        rendered = encoded[(left - 1) * 2:(right - 1) * 2].decode("utf-16-le")
        if name == "createParagraphBullets":
            presets.add(op["bulletPreset"])
            require(rendered.endswith("\n"))
            continue
        style = op["textStyle" if name == "updateTextStyle" else "paragraphStyle"]
        require(set(op["fields"].split(",")) == set(style) and "*" not in op["fields"])
        if "namedStyleType" in style:
            kinds.add(style["namedStyleType"])
        if "link" in style:
            links.append(style["link"]["url"])
            require(rendered == style["link"]["url"])
        if "weightedFontFamily" in style:
            code.append(style["weightedFontFamily"]["fontFamily"])
    require(kinds == {"NORMAL_TEXT", "TITLE", "HEADING_1"})
    require(presets == {"BULLET_DISC_CIRCLE_SQUARE", "NUMBERED_DECIMAL_ALPHA_ROMAN"})
    require(links and code and set(code) == {"Roboto Mono"})
    body = requests[1]["updateParagraphStyle"]
    require(body["range"] == {"startIndex": 11, "endIndex": 1 + len(encoded) // 2})
    require(body["paragraphStyle"] == {
        "namedStyleType": "NORMAL_TEXT", "lineSpacing": 115,
        "spaceAbove": {"magnitude": 0, "unit": "PT"},
        "spaceBelow": {"magnitude": 6, "unit": "PT"}})
    require(requests[2]["updateTextStyle"]["textStyle"] == {"fontSize": {"magnitude": 11, "unit": "PT"}})
    definition = requests[-2]["createDropdownDefinition"]["dropdownDefinition"]
    require(definition["dropdownDefinitionId"] == "kix.review_status")
    properties = definition["dropdownDefinitionProperties"]
    require(properties["title"] == "Review status")
    require([(o["optionId"], o["displayValue"]) for o in properties["options"]] == [
        ("dropdownItem.review", "review requested"),
        ("dropdownItem.changes", "changes requested"),
        ("dropdownItem.ready", "ready for implementation")])
    require(requests[-1] == {"insertDropdown": {"location": {"index": 9},
        "dropdownDefinitionId": "kix.review_status", "selectedOptionId": "dropdownItem.review"}})


def check_attention(fixture):
    """Validate synthetic contract examples, NOT a trusted event or authorization."""
    require(set(fixture) == {"prompt", "tokens", "rendered", "callback", "text"})
    prompt, tokens, rendered = (fixture[k] for k in ("prompt", "tokens", "rendered"))
    options = {o["id"]: o for o in prompt["options"]}
    require(2 <= len(options) <= 5 and len(options) == len(prompt["options"]))
    require(prompt["kind"] == "approval" and bool({"decline", "cancel"} & set(options)))
    require(prompt["recommended"] in options)
    require(set(tokens) == set(options) | {"__other__"} and len(set(tokens.values())) == len(tokens))
    require(all(isinstance(t, str) and re.fullmatch(r"a1:[A-Za-z0-9_-]{1,61}", t) for t in tokens.values()))
    require(rendered["parse_mode"] == "HTML")
    require('<b>Approval needed</b>' in rendered["text"])
    require('&lt;synthetic&gt; &amp; review' in rendered["text"])
    require('<synthetic>' not in rendered["text"] and 'use Reply' in rendered["text"])
    rows = rendered["reply_markup"]["inline_keyboard"]
    ordered = sorted(options.values(), key=lambda o: o["id"] != prompt["recommended"])
    require(rows == [[{"text": o["label"] + (" (Recommended)" if o["id"] == prompt["recommended"] else ""),
                       "callback_data": tokens[o["id"]]}] for o in ordered] +
            [[{"text": "Other / write a reply", "callback_data": tokens["__other__"]}]])
    require(rendered["text"].count("(Recommended)") == 1)
    require('Task paused.' in rendered["text"])
    callback, text = fixture["callback"], fixture["text"]
    common = {"question_id", "bot_id", "delivery_key", "pending", "context", "update_id"}
    require(set(callback) == common | {"schema_version", "callback_query", "update", "answer"})
    require(set(text) == common | {"reply"})
    for event in (callback, text):
        require(event["question_id"] == "synthetic-question-42" and event["bot_id"] == "123")
        require(event["pending"] == {"chat_id": 42, "user_id": 42, "message_id": 10})
        require(event["context"] == {"repository": "singleton11/hedgehoog", "issue_number": 42})
        require(type(event["update_id"]) is int)
        require(event["delivery_key"] == f'telegram:123:synthetic-question-42:{event["update_id"]}')
    require(type(callback["schema_version"]) is int and callback["schema_version"] == 1)
    raw = callback["update"]
    require(raw == {"update_id": callback["update_id"], "callback_query": callback["callback_query"]})
    query = callback["callback_query"]
    require(query["from"] == {"id": 42, "is_bot": False} and query["id"] == "synthetic-click")
    require(query["message"] == {"message_id": 10, "date": 100,
        "from": {"id": 123, "is_bot": True}, "chat": {"id": 42, "type": "private"}})
    answer = callback["answer"]
    require(answer["option_id"] in options and query["data"] == tokens[answer["option_id"]])
    option = options[answer["option_id"]]
    require(answer == {"source": "callback_query", "option_id": option["id"],
                       "label": option["label"], "text": option["description"]})
    reply = text["reply"]
    require(reply == {"message_id": 11, "chat": {"id": 42, "type": "private"},
        "from": {"id": 42, "is_bot": False}, "reply_to_message": {"message_id": 10},
        "text": "Keep this paused for human review."})


def check_repository(root):
    manifest = validate_manifest(json_bytes(read_regular(root, MANIFEST)))
    rows = []
    for path, expected in sorted(manifest["source_sha256"].items()):
        row = {"path": path, "expected_sha256": expected}
        try:
            actual = read_regular(root, path)
            row.update(actual_sha256=digest(actual), status="match" if digest(actual) == expected else "stale")
        except FileNotFoundError:
            row["status"] = "missing"
        except (OSError, ContractError):
            row["status"] = "unreadable_or_unsafe"
        rows.append(row)
    checks = []
    for name, path, validator in (("native_docs_fixture", PLAN, check_docs),
                                  ("html_callback_reply_fixture", FIXTURE, check_attention)):
        try:
            validator(json_bytes(read_regular(root, path)))
            checks.append({"contract": name, "status": "pass"})
        except (OSError, ValueError, KeyError, TypeError, IndexError, AttributeError):
            checks.append({"contract": name, "status": "fail"})
    return manifest, rows, checks


def check_snapshots(hashes=None, reader=None):
    rows = []
    for component, path in COMPONENTS.items():
        row = {"component": component, "path": path, "status": "not_inspected"}
        if hashes is not None:
            row["expected_sha256"] = hashes[path]
            try:
                actual = digest(reader(path))
                row.update(actual_sha256=actual, status="snapshot_hash_match" if actual == hashes[path] else "stale")
            except FileNotFoundError:
                row["status"] = "missing"
            except (OSError, ContractError):
                row["status"] = "unreadable_or_unsafe"
        rows.append(row)
    return rows


def make_report(root=ROOT, hashes=None, reader=None, repository_only=False, demo=False):
    manifest, sources, checks = check_repository(root)
    snapshots = check_snapshots(hashes, reader)
    repository_ok = all(r["status"] == "match" for r in sources) and all(c["status"] == "pass" for c in checks)
    snapshots_ok = all(r["status"] == "snapshot_hash_match" for r in snapshots)
    status = "offline_ok" if repository_ok and snapshots_ok else "blocked"
    if repository_only and repository_ok:
        status = "repository_ok"
    return {"schema_version": 1, "runtime": "temporal", "mode": "synthetic_demo" if demo else "inventory",
            "status": status, "repository": sources, "fixture_checks": checks, "snapshots": snapshots,
            "mapping": manifest["components"], "limitations": LIMITATIONS,
            "external_behavior": "not_verified", "deployment": "not_performed"}


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse normally echoes unknown arguments, which may include private paths.
        self.exit(2, '{"error":"invalid_cli_arguments"}\n')


def main(argv=None):
    parser = SafeParser(description=__doc__)
    parser.add_argument("--repository-only", action="store_true", help="check fixtures only; not deployment readiness")
    parser.add_argument("--snapshot-root", type=Path, help="explicit sanitized snapshot root, never a live installation")
    parser.add_argument("--snapshot-lock", type=Path, help="independently reviewed Temporal SHA-256 JSON lock")
    parser.add_argument("--demo", action="store_true", help="in-memory synthetic stale/missing snapshot demonstration")
    args = parser.parse_args(argv)
    if (bool(args.snapshot_root) != bool(args.snapshot_lock) or
            (args.demo and (args.snapshot_root or args.repository_only)) or
            (args.repository_only and args.snapshot_root)):
        print('{"error":"choose_repository_only_demo_or_paired_snapshot_arguments"}')
        return 2
    try:
        hashes, reader = None, None
        if args.demo:
            data = {p: ("# synthetic offline snapshot: " + n + "\n").encode() for n, p in COMPONENTS.items()}
            hashes = {p: digest(b) for p, b in data.items()}
            data[COMPONENTS["attention"]] += b"# stale fixture\n"
            del data[COMPONENTS["service"]]

            def reader(path):
                if path not in data:
                    raise FileNotFoundError
                return data[path]
        elif args.snapshot_root:
            hashes = validate_snapshot_lock(json_bytes(read_regular(args.snapshot_lock.parent, args.snapshot_lock.name)))
            reader = lambda path: read_regular(args.snapshot_root, path)
        report = make_report(hashes=hashes, reader=reader, repository_only=args.repository_only, demo=args.demo)
    except (OSError, ValueError, KeyError, TypeError, IndexError, AttributeError):
        print('{"error":"invalid_or_unreadable_inventory_input","deployment":"not_performed"}')
        return 2
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] in {"repository_ok", "offline_ok"} else 1


if __name__ == "__main__":
    sys.exit(main())
