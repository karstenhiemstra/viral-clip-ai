#!/usr/bin/env python3
"""Fail if something that looks like a real secret is (about to be) committed.

    python3 scripts/check_secrets.py            # scan all tracked files (CI)
    python3 scripts/check_secrets.py --staged   # scan staged changes (git pre-commit hook)

Install as pre-commit hook with ``make hooks``. Uses only the standard library.
"""

from __future__ import annotations

import re
import subprocess
import sys

PATTERNS = {
    "OpenAI API key": re.compile(r"\bsk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_-]{32,}"),
    "Anthropic API key": re.compile(r"\bsk-ant-[a-z]+\d{2}-[A-Za-z0-9_-]{20,}"),
    "Google API key": re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    "Private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    "AWS access key": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "GitHub token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
}
# KEY=value lines in env-style files: the value must be empty or an obvious placeholder.
ENV_SECRET = re.compile(r"^[ \t]*([A-Z0-9_]*(?:API_KEY|SECRET|TOKEN|PASSWORD)[A-Z0-9_]*)[ \t]*=[ \t]*([^\s#]+)", re.M)
PLACEHOLDER = re.compile(r"^(?:\$\{.*\}|<.*>|changeme|change-me|xxx+|\.\.\.|\"\"|'')$", re.I)
SKIP_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".mp4", ".mp3", ".woff", ".woff2", ".ttf", ".onnx", ".lock")


def git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout


def main() -> int:
    staged = "--staged" in sys.argv
    files = git("diff", "--cached", "--name-only", "--diff-filter=ACM").split() if staged else git("ls-files").split()
    problems: list[str] = []
    for f in files:
        name = f.rsplit("/", 1)[-1]
        if name == ".env" or (name.startswith(".env.") and name != ".env.example"):
            problems.append(f"{f}: .env-bestanden horen niet in Git (staan in .gitignore)")
            continue
        if f.endswith(SKIP_SUFFIXES):
            continue
        try:
            text = git("show", f":{f}") if staged else open(f, encoding="utf-8", errors="ignore").read()
        except (OSError, subprocess.CalledProcessError):
            continue
        for label, rx in PATTERNS.items():
            for m in rx.finditer(text):
                line = text.count("\n", 0, m.start()) + 1
                problems.append(f"{f}:{line}: mogelijke {label} ({m.group(0)[:8]}…)")
        if name == ".env.example" or name.endswith(".env"):
            for m in ENV_SECRET.finditer(text):
                if not PLACEHOLDER.match(m.group(2)):
                    line = text.count("\n", 0, m.start()) + 1
                    problems.append(f"{f}:{line}: {m.group(1)} heeft een waarde; laat deze leeg in voorbeeldbestanden")
    if problems:
        print("Mogelijke secrets gevonden — commit/CI gestopt:\n  " + "\n  ".join(problems))
        print("Zet echte keys alleen in .env (staat in .gitignore) of in Instellingen van de app.")
        return 1
    print(f"Geen secrets gevonden in {len(files)} bestanden.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
