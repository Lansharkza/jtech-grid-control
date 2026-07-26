#!/usr/bin/env python3
"""Static checks for the dashboard pages.

The Content-Security-Policy has no 'unsafe-inline', so inline style attributes
and inline event handlers are silently dropped by the browser — the page still
loads, it just quietly stops working. That failure is invisible to a normal
smoke test, so it is checked here instead.

Run before packaging:  python check.py
"""

import re
import sys
from pathlib import Path

PAGES = ["static/index.html", "static/login.html"]
BUILTIN_ACTIONS = {"clearLimit", "solarOff", "reset", "availability",
                   "setMode", "setAutoStart", "savePrice", "saveVehicle"}

failures: list[str] = []


def fail(page: str, message: str) -> None:
    failures.append(f"{page}: {message}")


for page in PAGES:
    src = Path(page).read_text(encoding="utf-8")

    # 1. CSP: inline styles and handlers are blocked
    for match in re.findall(r'\sstyle="[^"]*"', src):
        fail(page, f"inline style attribute blocked by CSP -> {match.strip()[:60]}")
    for match in re.findall(r'\son(?:click|change|input|submit|load)="[^"]*"', src):
        fail(page, f"inline event handler blocked by CSP -> {match.strip()[:60]}")

    # 2. markup: unterminated tags swallow the stylesheet
    markup = re.sub(r"<(style|script)[^>]*>.*?</\1>", "", src, flags=re.S)
    state, quote = "text", ""
    for char in markup:
        if state == "text" and char == "<":
            state = "tag"
        elif state == "tag":
            if quote:
                quote = "" if char == quote else quote
            elif char in "\"'":
                quote = char
            elif char == ">":
                state = "text"
    if state != "text":
        fail(page, "unterminated tag — the stylesheet will be swallowed")

    if "<style>" not in src:
        fail(page, "no <style> block found")

    script = re.search(r"<script>(.*)</script>", src, re.S)
    if not script:
        fail(page, "no <script> block found")
        continue
    js = script.group(1)

    # 3. every $('id') must exist in the markup
    ids = set(re.findall(r'id="([\w-]+)"', src))
    for ref in set(re.findall(r"\$\('([\w-]+)'\)", js)):
        if ref not in ids:
            fail(page, f"script references #{ref}, which is not in the page")

    # 4. every data-act must resolve to something in ACTIONS
    block = re.search(r"const ACTIONS = \{(.*?)\n\};", js, re.S)
    if block:
        declared = set(re.findall(r"(?:async\s+)?function\s+(\w+)", js))
        declared |= set(re.findall(r"const\s+(\w+)\s*=", js))
        declared |= set(re.findall(r"(\w+):\s*(?:async\s+)?\w*\s*=>", js))
        # Split the object into top-level entries, tracking depth so nested
        # object literals are not mistaken for action names.
        entries, depth, token = [], 0, ""
        for char in block.group(1):
            if char in "{[(":
                depth += 1
            elif char in "}])":
                depth -= 1
            if depth == 0 and char == ",":
                entries.append(token)
                token = ""
            else:
                token += char
        entries.append(token)

        names = set()
        for entry in entries:
            entry = entry.split("//")[0].strip()
            if not entry:
                continue
            if ":" in entry:
                key, value = entry.split(":", 1)
                key, value = key.strip(), value.strip()
                if not re.fullmatch(r"\w+", key):
                    continue
                names.add(key)
                # `name: otherFunction` is an alias — the target must exist.
                if re.fullmatch(r"\w+", value) and value not in BUILTIN_ACTIONS:
                    if value not in declared:
                        fail(page, f"ACTIONS.{key} points at {value}, which is "
                                   "not defined")
            elif re.fullmatch(r"\w+", entry):
                names.add(entry)
                if entry not in declared and entry not in BUILTIN_ACTIONS:
                    fail(page, f"ACTIONS.{entry} has no matching function — "
                               "this throws and kills the whole script")

        handled = names | BUILTIN_ACTIONS
        for act in set(re.findall(r'data-act="(\w+)"', src)):
            if act not in handled:
                fail(page, f'data-act="{act}" has no handler')

    # 5. classes used by the script must be defined in the stylesheet
    css = re.search(r"<style>(.*?)</style>", src, re.S).group(1)
    for cls in set(re.findall(r"'(c-[\w-]+|cfg-\w+|txt-\w+)'", js)):
        if f".{cls}" not in css:
            fail(page, f"class .{cls} used by the script but not defined in CSS")

if failures:
    print("FAILED")
    for line in failures:
        print("  " + line)
    sys.exit(1)

print(f"OK — {len(PAGES)} pages pass: no inline styles or handlers, markup balanced, "
      "all references resolve")
