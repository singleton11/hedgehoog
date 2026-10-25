#!/usr/bin/env python3
"""Build a Google Docs batchUpdate body from structured plan JSON (stdlib only).

Input: {"title": str, "issue_url": https_url, "sections": [
  {"heading": str, "paragraphs": [str | {"text": str, "list": "bullet" | "numbered"}]}]}
All strings support `code` and [label](https://url) inline markup, without nesting.
An empty paragraph string creates a blank line. Sections may be empty or omitted.
Unknown fields, block Markdown, multiline text, and nested lists are rejected.
"""

import copy
import json
import re
import sys
import unicodedata
from pathlib import Path
from urllib.parse import urlsplit

PREFIX = "Status: \n\n"
# Kept verbatim (as JSON data) from the original planning template.
DROPDOWN_REQUESTS = json.loads(r'''[
  {
    "createDropdownDefinition": {
      "dropdownDefinition": {
        "dropdownDefinitionId": "kix.review_status",
        "dropdownDefinitionProperties": {
          "title": "Review status",
          "options": [
            {
              "optionId": "dropdownItem.review",
              "displayValue": "review requested",
              "textStyle": {
                "backgroundColor": {
                  "color": {
                    "rgbColor": {
                      "red": 0.85,
                      "green": 0.92,
                      "blue": 1
                    }
                  }
                },
                "foregroundColor": {
                  "color": {
                    "rgbColor": {
                      "red": 0.1,
                      "green": 0.25,
                      "blue": 0.6
                    }
                  }
                }
              }
            },
            {
              "optionId": "dropdownItem.changes",
              "displayValue": "changes requested",
              "textStyle": {
                "backgroundColor": {
                  "color": {
                    "rgbColor": {
                      "red": 1,
                      "green": 0.88,
                      "blue": 0.8
                    }
                  }
                },
                "foregroundColor": {
                  "color": {
                    "rgbColor": {
                      "red": 0.6,
                      "green": 0.2,
                      "blue": 0.05
                    }
                  }
                }
              }
            },
            {
              "optionId": "dropdownItem.ready",
              "displayValue": "ready for implementation",
              "textStyle": {
                "backgroundColor": {
                  "color": {
                    "rgbColor": {
                      "red": 0.83,
                      "green": 0.94,
                      "blue": 0.85
                    }
                  }
                },
                "foregroundColor": {
                  "color": {
                    "rgbColor": {
                      "red": 0.1,
                      "green": 0.4,
                      "blue": 0.18
                    }
                  }
                }
              }
            }
          ]
        }
      }
    }
  },
  {
    "insertDropdown": {
      "location": {
        "index": 9
      },
      "dropdownDefinitionId": "kix.review_status",
      "selectedOptionId": "dropdownItem.review"
    }
  }
]''')


def utf16_length(text):
    return len(text.encode("utf-16-le")) // 2


def fields(value, required, optional=()):
    if not isinstance(value, dict):
        raise ValueError("expected an object")
    if set(value) - set(required) - set(optional) or set(required) - set(value):
        raise ValueError("missing or unknown fields: " + repr(list(value)))


def plain_string(value):
    if not isinstance(value, str):
        raise ValueError("text must be a string")
    # Docs strips BMP private-use characters, invalidating calculated ranges.
    if any("\ue000" <= c <= "\uf8ff" for c in value):
        raise ValueError("text must not contain BMP private-use characters")
    if any(unicodedata.category(c) in {"Cc", "Cs", "Cf", "Zl", "Zp"} for c in value):
        raise ValueError("text must be a single line without control characters")
    if (value.startswith("    ") or
            re.match(r"^\s*(?:#{1,6}(?:\s|$)|[-+*]\s|\d+[.)]\s|>|```|~~~|\|)", value) or
            re.fullmatch(r"\s*(?:[-*_]\s*){3,}|\s*=+\s*", value)):
        raise ValueError("block Markdown is not supported; use structured paragraphs/lists")
    if value and not value.strip():
        raise ValueError("use an empty string for a blank paragraph")
    return value


def url(value):
    if not isinstance(value, str) or any(c.isspace() or ord(c) < 32 for c in value):
        raise ValueError("links must be absolute HTTP(S) URLs")
    try:
        parsed = urlsplit(value)
        valid = parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username and not parsed.password
        parsed.port
    except ValueError:
        valid = False
    if not valid or any(c in value for c in "<>\\`[]()"):
        raise ValueError("links must be absolute HTTP(S) URLs without credentials or markup")
    return value


def reject_emphasis(text):
    if re.search(r"\*\*.+?\*\*|__.+?__|(?<!\w)\*[^*]+\*(?!\w)|(?<!\w)_[^_]+_(?!\w)", text):
        raise ValueError("emphasis Markdown is not supported; use plain text")


def inline(value):
    """Return rendered text and zero-based UTF-16 inline style ranges."""
    value = plain_string(value)
    parts, spans = [], []
    index = 0
    length = 0
    while index < len(value):
        char = value[index]
        if char == "`":
            end = value.find("`", index + 1)
            if end < 0 or end == index + 1:
                raise ValueError("code spans must have nonempty, matched single backticks")
            text = value[index + 1:end]
            style = {"weightedFontFamily": {"fontFamily": "Roboto Mono"}}
            index = end + 1
        elif char == "[":
            match = re.match(r"\[([^\[\]`]+)\]\(([^()]+)\)", value[index:])
            if not match:
                raise ValueError("links must use [plain label](https://url), without nesting")
            text = match[1]
            reject_emphasis(text)
            style = {"link": {"url": url(match[2])}}
            index += match.end()
        elif char == "]":
            raise ValueError("unmatched link marker")
        else:
            end = index + 1
            while end < len(value) and value[end] not in "`[]":
                end += 1
            text, style = value[index:end], None
            reject_emphasis(text)
            index = end
        size = utf16_length(text)
        if style:
            spans.append((length, length + size, style))
        parts.append(text)
        length += size
    return "".join(parts), spans


def build_plan(content):
    fields(content, {"title", "issue_url"}, {"sections"})
    issue_url = url(content["issue_url"])
    sections = content.get("sections", [])
    if not isinstance(sections, list):
        raise ValueError("sections must be an array")
    paragraphs = []

    def add(text, kind="NORMAL_TEXT", list_type=None):
        rendered, spans = inline(text)
        if kind != "NORMAL_TEXT" and not rendered.strip():
            raise ValueError("title and headings must not be empty")
        if list_type and not rendered.strip():
            raise ValueError("list items must not be empty")
        paragraphs.append((rendered, spans, kind, list_type))

    add(content["title"], "TITLE")
    add("GitHub issue: [" + issue_url + "](" + issue_url + ")")
    add("")
    for section in sections:
        fields(section, {"heading", "paragraphs"})
        if not isinstance(section["paragraphs"], list):
            raise ValueError("paragraphs must be an array")
        add(section["heading"], "HEADING_1")
        for paragraph in section["paragraphs"]:
            if isinstance(paragraph, str):
                add(paragraph)
            else:
                fields(paragraph, {"text"}, {"list"})
                list_type = paragraph.get("list")
                if "list" in paragraph and list_type not in ("bullet", "numbered"):
                    raise ValueError("list must be bullet or numbered")
                add(paragraph["text"], list_type=list_type)

    text = PREFIX + "".join(p[0] + "\n" for p in paragraphs)
    start = 1 + utf16_length(PREFIX)
    end = 1 + utf16_length(text)
    whole = {"startIndex": start, "endIndex": end}
    requests = [{"insertText": {"location": {"index": 1}, "text": text}}]
    requests.append({"updateParagraphStyle": {
        "range": whole, "paragraphStyle": {
            "namedStyleType": "NORMAL_TEXT", "lineSpacing": 115,
            "spaceAbove": {"magnitude": 0, "unit": "PT"},
            "spaceBelow": {"magnitude": 6, "unit": "PT"}},
        "fields": "namedStyleType,lineSpacing,spaceAbove,spaceBelow"}})
    requests.append({"updateTextStyle": {
        "range": whole, "textStyle": {"fontSize": {"magnitude": 11, "unit": "PT"}},
        "fields": "fontSize"}})
    lists = []
    cursor = start
    for rendered, spans, kind, list_type in paragraphs:
        stop = cursor + utf16_length(rendered) + 1
        if kind != "NORMAL_TEXT":
            requests.append({"updateParagraphStyle": {
                "range": {"startIndex": cursor, "endIndex": stop},
                "paragraphStyle": {"namedStyleType": kind,
                                   "spaceAbove": {"magnitude": 12, "unit": "PT"},
                                   "spaceBelow": {"magnitude": 6, "unit": "PT"}},
                "fields": "namedStyleType,spaceAbove,spaceBelow"}})
            requests.append({"updateTextStyle": {
                "range": {"startIndex": cursor, "endIndex": stop - 1},
                "textStyle": {"fontSize": {"magnitude": 22 if kind == "TITLE" else 14,
                                           "unit": "PT"}},
                "fields": "fontSize"}})
        for left, right, style in spans:
            requests.append({"updateTextStyle": {
                "range": {"startIndex": cursor + left, "endIndex": cursor + right},
                "textStyle": style, "fields": ",".join(style)}})
        if list_type:
            if lists and lists[-1][0] == list_type and lists[-1][2] == cursor:
                lists[-1][2] = stop
            else:
                lists.append([list_type, cursor, stop])
        cursor = stop
    # There are no tabs, so bullet creation cannot shift the precomputed ranges.
    for kind, left, right in lists:
        requests.append({"createParagraphBullets": {
            "range": {"startIndex": left, "endIndex": right},
            "bulletPreset": "BULLET_DISC_CIRCLE_SQUARE" if kind == "bullet" else "NUMBERED_DECIMAL_ALPHA_ROMAN"}})
    requests.extend(copy.deepcopy(DROPDOWN_REQUESTS))
    return {"requests": requests}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("usage: build_plan.py /path/to/plan-content.json", file=sys.stderr)
        return 2
    try:
        content = json.loads(Path(argv[0]).read_text(encoding="utf-8"))
        result = build_plan(content)
    except (OSError, ValueError, TypeError) as exc:
        print("build_plan: " + str(exc), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
