"""A deterministic fake LLM that answers both passes based on the prompt content."""

from __future__ import annotations

import re

from app.ai.llm import LLMResult, LLMUsage
from app.ai.scoring import DIMENSIONS


class FakeLLM:
    provider = "fake"
    models = {"fast": "fake-fast", "smart": "fake-smart", "vision": "fake-smart"}

    def __init__(self, keywords=("wacht", "banaan", "miljoen", "loterij", "fake")):
        self.keywords = keywords
        self.calls: list[str] = []

    def complete_json(self, *, system, user, schema, schema_name, tier="smart", max_tokens=8000, images=None):
        self.calls.append(schema_name)
        usage = LLMUsage(model=self.models.get(tier, "fake"), input_tokens=1000, output_tokens=200, cost_usd=0.001)
        if schema_name == "moments":
            moments = []
            for m in re.finditer(r"\[s(\d+) [^\]]+\] (.*)", user):
                idx, text = int(m.group(1)), m.group(2).lower()
                if any(k in text for k in self.keywords):
                    moments.append(
                        {"start_sentence": idx, "end_sentence": idx + 1, "hook_sentence": idx, "category": "reaction",
                         "description": "Sterk moment", "reason": "Directe hook", "strength": 8}
                    )
            return LLMResult({"moments": moments}, usage)
        if schema_name == "evaluations":
            evals = []
            for block in re.split(r"=== CANDIDATE ", user)[1:]:
                cid = block.split(" ", 1)[0]
                clip_part = block.split("CLIP:", 1)[1].split("CONTEXT AFTER", 1)[0]
                ids = [int(x) for x in re.findall(r"\[s(\d+) ", clip_part)]
                hot = any(k in clip_part.lower() for k in self.keywords)
                base = 85 if hot else 40
                evals.append(
                    {
                        "id": cid, "first_seconds": "Wacht wat", "viewer_reaction": "Ik blijf kijken",
                        "scores": dict.fromkeys(DIMENSIONS, base), "flags": [] if hot else ["weak_payoff"],
                        "verdict": "great" if hot else "maybe", "start_sentence": ids[0], "end_sentence": ids[-1],
                        "category": "reaction", "title": "Dit geloof je niet", "why": "Sterke hook en payoff.",
                        "hook_line": "Wacht wat", "emphasis_words": ["banaan", "miljoen", "nietbestaand"],
                    }
                )
            return LLMResult({"evaluations": evals}, usage)
        if schema_name == "health":
            return LLMResult({"ok": True}, usage)
        return LLMResult({}, usage)

    def embed(self, texts):
        return None
