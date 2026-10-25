import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/build_plan.py"
SPEC = importlib.util.spec_from_file_location("build_plan", SCRIPT)
plan = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(plan)


def sample(paragraphs=None):
    return {"title": "A plan", "issue_url": "https://example.org/issues/35",
            "sections": [{"heading": "Goal", "paragraphs": paragraphs or []}]}


def slice_utf16(text, start, end):
    return text.encode("utf-16-le")[(start - 1) * 2:(end - 1) * 2].decode("utf-16-le")


class BuildPlanTests(unittest.TestCase):
    def test_exact_rendering(self):
        result = plan.build_plan(sample(["Use `thing.py` and [docs](https://example.org/docs).", "", "Done."]))
        self.assertEqual(result["requests"][0]["insertText"]["text"],
                         "Status: \n\nA plan\nGitHub issue: https://example.org/issues/35\n\nGoal\nUse thing.py and docs.\n\nDone.\n")

    def test_optional_sections(self):
        for sections in (None, []):
            value = {"title": "Title", "issue_url": "http://example.org"}
            if sections is not None:
                value["sections"] = sections
            self.assertEqual(plan.build_plan(value)["requests"][0]["insertText"]["text"],
                             "Status: \n\nTitle\nGitHub issue: http://example.org\n\n")

    def test_utf16_ranges_and_long_text(self):
        for body in ("x", "x" * 20000, "\U0001f680 cafe\u0301 `\U0001f9aa.py` [\U0001f600 docs](https://example.org)"):
            value = sample([body])
            value["title"] = "Plan \U0001f680"
            requests = plan.build_plan(value)["requests"]
            text = requests[0]["insertText"]["text"]
            for request in requests[1:-2]:
                op = next(iter(request.values()))
                interval = op["range"]
                self.assertGreaterEqual(interval["startIndex"], 10)
                self.assertLessEqual(interval["endIndex"], 1 + plan.utf16_length(text))
                rendered = slice_utf16(text, interval["startIndex"], interval["endIndex"])
                if op.get("fields") == "weightedFontFamily":
                    self.assertEqual(rendered, "\U0001f9aa.py")
                if op.get("textStyle", {}).get("link", {}).get("url") == "https://example.org":
                    self.assertEqual(rendered, "\U0001f600 docs")
            self.assertEqual(requests[1]["updateParagraphStyle"]["range"]["endIndex"],
                             1 + plan.utf16_length(text))

    def test_list_boundaries(self):
        value = sample([{"text": "one", "list": "bullet"}, {"text": "two", "list": "bullet"},
                        "", {"text": "three", "list": "bullet"},
                        {"text": "four", "list": "numbered"}, "plain",
                        {"text": "five", "list": "numbered"}])
        value["sections"].append({"heading": "Next", "paragraphs": [{"text": "six", "list": "numbered"}]})
        requests = plan.build_plan(value)["requests"]
        text = requests[0]["insertText"]["text"]
        bullets = [r["createParagraphBullets"] for r in requests if "createParagraphBullets" in r]
        self.assertEqual([slice_utf16(text, b["range"]["startIndex"], b["range"]["endIndex"]) for b in bullets],
                         ["one\ntwo\n", "three\n", "four\n", "five\n", "six\n"])
        self.assertEqual([b["bulletPreset"] for b in bullets],
                         ["BULLET_DISC_CIRCLE_SQUARE"] * 2 + ["NUMBERED_DECIMAL_ALPHA_ROMAN"] * 3)
        self.assertNotIn("\t", text)

    def test_order_and_masks(self):
        requests = plan.build_plan(sample([{"text": "`file` [docs](https://example.org)", "list": "bullet"}]))["requests"]
        self.assertEqual(list(requests[0]), ["insertText"])
        self.assertEqual([list(r)[0] for r in requests[-3:]],
                         ["createParagraphBullets", "createDropdownDefinition", "insertDropdown"])
        for request in requests:
            for kind in ("updateTextStyle", "updateParagraphStyle"):
                if kind in request:
                    op = request[kind]
                    style = op["textStyle" if kind == "updateTextStyle" else "paragraphStyle"]
                    self.assertEqual(set(op["fields"].split(",")), set(style))
                    self.assertNotIn("*", op["fields"])
        self.assertEqual(requests[1]["updateParagraphStyle"]["paragraphStyle"]["lineSpacing"], 115)
        self.assertEqual(requests[2]["updateTextStyle"]["textStyle"]["fontSize"]["magnitude"], 11)
        self.assertEqual([r["updateParagraphStyle"]["paragraphStyle"]["namedStyleType"]
                          for r in requests if "updateParagraphStyle" in r],
                         ["NORMAL_TEXT", "TITLE", "HEADING_1"])

    def test_dropdown_contract(self):
        requests = plan.build_plan(sample())["requests"]
        self.assertEqual(requests[-1], {"insertDropdown": {"location": {"index": 9},
                         "dropdownDefinitionId": "kix.review_status", "selectedOptionId": "dropdownItem.review"}})
        definition = requests[-2]["createDropdownDefinition"]["dropdownDefinition"]
        self.assertEqual(definition["dropdownDefinitionId"], "kix.review_status")
        self.assertEqual(definition["dropdownDefinitionProperties"]["title"], "Review status")
        options = definition["dropdownDefinitionProperties"]["options"]
        expected = [("review", "review requested", (.85, .92, 1), (.1, .25, .6)),
                    ("changes", "changes requested", (1, .88, .8), (.6, .2, .05)),
                    ("ready", "ready for implementation", (.83, .94, .85), (.1, .4, .18))]
        for option, (name, label, background, foreground) in zip(options, expected):
            self.assertEqual(option["optionId"], "dropdownItem." + name)
            self.assertEqual(option["displayValue"], label)
            for key, color in (("backgroundColor", background), ("foregroundColor", foreground)):
                self.assertEqual(option["textStyle"][key]["color"]["rgbColor"], dict(zip(("red", "green", "blue"), color)))
        self.assertEqual(len(options), 3)
        requests[-1]["insertDropdown"]["location"]["index"] = 999
        self.assertEqual(plan.build_plan(sample())["requests"][-1]["insertDropdown"]["location"]["index"], 9)

    def test_inline_parser(self):
        text, spans = plan.inline("`a` then [b](https://example.org/x?q=1#part) and `c`")
        self.assertEqual(text, "a then b and c")
        self.assertEqual([(a, b) for a, b, _ in spans], [(0, 1), (7, 8), (13, 14)])
        self.assertNotIn("`", text)
        self.assertNotIn("](", text)

    def test_reject_unsafe_text(self):
        for text in ("# heading", "- item", "+ item", "* item", "1. item", "2) item", "> quote",
                     "```code```", "~~~code", "| table |", "---", "***", "___", "===", "    code",
                     "**bold**", "Use *emphasis* here", "__bold__", "Use _emphasis_ here",
                     "[**docs**](https://example.org)", "[_docs_](https://example.org)",
                     "bad\nline", "bad\titem", "\x00", "\u2028",
                     "\u202e", "\ud800", "   ", "`open", "``", "[label](javascript:alert)",
                     "[label](file:///tmp/x)", "[label](data:text/plain,test)", "[x]", "[a `b`](https://example.org)"):
            with self.subTest(text=repr(text)), self.assertRaises(ValueError):
                plan.build_plan(sample([text]))

    def test_reject_bmp_private_use_before_rendering(self):
        for char in ("\ue000", "\ue001", "\uf8fe", "\uf8ff"):
            values = [
                {**sample(), "title": "Plan " + char},
                {**sample(), "issue_url": "https://example.org/" + char},
                {**sample(), "sections": [{"heading": "Goal " + char, "paragraphs": []}]},
            ]
            for text in (char, "before " + char + " `later.py`",
                         "`file" + char + ".py`",
                         "[label" + char + "](https://example.org)"):
                values.append(sample([text]))
                values.append(sample([{"text": text, "list": "bullet"}]))
            for value in values:
                with self.subTest(char=repr(char), value=value):
                    with self.assertRaisesRegex(ValueError, "BMP private-use"):
                        plan.build_plan(value)

    def test_preserve_characters_outside_bmp_private_use(self):
        text = "\ud7ff\uf900\U000f0000\U0010fffd `later.py`"
        requests = plan.build_plan(sample([text]))["requests"]
        inserted = requests[0]["insertText"]["text"]
        self.assertIn(text.replace("`", ""), inserted)
        code = next(r["updateTextStyle"] for r in requests
                    if r.get("updateTextStyle", {}).get("fields") == "weightedFontFamily")
        self.assertEqual(slice_utf16(inserted, code["range"]["startIndex"],
                                     code["range"]["endIndex"]), "later.py")

    def test_reject_unsafe_urls(self):
        for link in ("javascript:bad", "//example.org", "https://", "https://user:pw@example.org",
                     "https://example.org/a b", "https://example.org/\n", "https://[", "https://example.org:bad"):
            value = sample()
            value["issue_url"] = link
            with self.subTest(link=link), self.assertRaises(ValueError):
                plan.build_plan(value)

    def test_reject_schema_and_list_controls(self):
        bad = [None, [], {}, {**sample(), "unknown": 1}, {**sample(), "title": ""},
               {**sample(), "title": 42}, {**sample(), "sections": {}},
               {**sample(), "sections": [{"heading": "", "paragraphs": []}]},
               {**sample(), "sections": [{"heading": "ok", "paragraphs": "bad"}]}]
        for paragraph in ({"text": "x", "list": "nested"}, {"text": "x", "list": None},
                          {"text": "x", "list": "bullet", "level": 2}, {"text": "", "list": "bullet"},
                          {"text": "x", "list": []}, 1, ["x"], {"raw": "x"}):
            bad.append(sample([paragraph]))
        for value in bad:
            with self.subTest(value=value), self.assertRaises(ValueError):
                plan.build_plan(value)

    def test_template_parity_and_cli(self):
        source = ROOT / "references/plan-content.json"
        expected = json.loads((ROOT / "references/create-plan.json").read_text())
        self.assertEqual(plan.build_plan(json.loads(source.read_text())), expected)
        result = subprocess.run([sys.executable, str(SCRIPT), str(source)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), expected)
        self.assertEqual(result.stderr, "")

    def test_cli_errors(self):
        for args in ([], [str(ROOT / "nonexistent.json")], [str(SCRIPT)]):
            result = subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, "")
            self.assertTrue(result.stderr)
            self.assertNotIn("Traceback", result.stderr)

    def test_no_mutation(self):
        source = sample([{"text": "`file`", "list": "bullet"}])
        original = copy.deepcopy(source)
        plan.build_plan(source)
        self.assertEqual(source, original)


if __name__ == "__main__":
    unittest.main()
