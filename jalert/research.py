"""Two-stage, configurable storage relevance: contextual topics then bounded scoring.

Legacy scoring is untouched unless research.enabled is set. This module never
changes paper identities or history, and gives each paper one primary category.
"""
from collections import defaultdict
import re
import unicodedata

from .score import Match, Scored


def normalized(text):
    text = unicodedata.normalize("NFKC", text or "").casefold()
    return re.sub(r"\s+", " ", re.sub(r"[‐‑‒–—−-]", " ", text)).strip()


def pattern(term):
    term = normalized(term)
    return re.compile(r"(?<!\w)" + re.escape(term) + r"(?!\w)")


class ResearchMatcher:
    def __init__(self, config):
        self.cfg = config
        self.radius = max(0, int(config.get("context_chars", 140)))
        self.contexts = {key: [(t, pattern(t)) for t in terms]
                         for key, terms in config.get("contexts", {}).items()}
        self.context_exclusions = {key: [pattern(t) for t in terms]
                                   for key, terms in config.get("context_exclusions", {}).items()}
        self.categories = config.get("categories", [])
        self.rules = []
        self.topic_labels = {}
        self.topic_categories = {}
        weights = config.get("weights", {"core": 5, "technical": 4, "extended": 2})
        for category in self.categories:
            for topic in category.get("subtopics", []):
                if not topic.get("enabled", True):
                    continue
                topic_id = topic.get("topic_id", topic["id"])
                self.topic_labels[topic_id] = topic.get("topic_label", topic["label"])
                self.topic_categories[topic_id] = category["id"]
                for signal in topic.get("signals", []):
                    level = signal["level"]
                    self.rules.append(dict(signal, topic=topic_id, label=topic["label"],
                        weight=int(signal.get("weight", weights[level])),
                        compiled=[(t, pattern(t)) for t in signal["terms"]],
                        negative=[pattern(t) for t in signal.get("exclude_context", [])]))

    def context(self, name, text):
        if any(p.search(text) for p in self.context_exclusions.get(name, [])):
            return ""
        return next((term for term, regex in self.contexts.get(name, []) if regex.search(text)), "")

    def score(self, item):
        result = Scored()
        fields = {"title": normalized(item.title), "abstract": normalized(item.abstract)}
        evidence = []
        # Stage 1: all validated concepts may classify a paper, even when their
        # overlapping spans will be counted only once during stage 2.
        for rule in self.rules:
            candidates = []
            rejected = False
            for field_name, text in fields.items():
                for term, regex in rule["compiled"]:
                    for hit in regex.finditer(text):
                        local = text[max(0, hit.start() - self.radius):hit.end() + self.radius]
                        context = self.context(rule.get("context", ""), local)
                        if (rule.get("context") and not context) or any(p.search(local) for p in rule["negative"]):
                            rejected = True
                            continue
                        device = self.context("device", local)
                        storage = self.context("storage_architecture", local)
                        linked = bool(device or storage or (context and rule.get("context") in {"device", "nand", "storage_architecture"}))
                        weight = rule["weight"]
                        if rule["level"] == "extended" and linked:
                            weight = int(rule.get("linked_weight", self.cfg.get("extended_linked_weight", 4)))
                        multiplier = int(self.cfg.get("title_multiplier", 3) if field_name == "title" else self.cfg.get("abstract_multiplier", 1))
                        candidates.append(dict(rule=rule, term=term, field=field_name,
                            span=hit.span(), points=weight * multiplier,
                            context=context or device or storage, linked=linked))
            if candidates:
                # One best occurrence per concept/rule: title wins, then length.
                evidence.append(max(candidates, key=lambda e: (e["field"] == "title", e["points"], e["span"][1] - e["span"][0])))
            elif rejected:
                result.rejected.append(f"{rule['id']}: missing required context or ambiguous meaning")

        classified = [e for e in evidence if not e["rule"].get("fallback", False)] or evidence
        topic_points = defaultdict(int)
        for e in classified:
            topic_points[e["rule"]["topic"]] = max(topic_points[e["rule"]["topic"]], e["points"])
        result.topics = [t for t in self.topic_labels if t in topic_points]
        result.topic_labels = [self.topic_labels[t] for t in result.topics]
        if result.topics:
            result.primary_topic = max(result.topics, key=lambda t: topic_points[t])
            result.category = self.topic_categories[result.primary_topic]

        # Stage 2: shared concept IDs and overlapping terms never accumulate.
        occupied = defaultdict(list)
        concepts = set()
        for e in sorted(evidence, key=lambda e: (e["field"] == "title", e["points"], e["span"][1] - e["span"][0]), reverse=True):
            rule = e["rule"]
            start, end = e["span"]
            if rule["id"] in concepts:
                continue
            concepts.add(rule["id"])
            if any(start < b and a < end for a, b in occupied[e["field"]]):
                # Preserve topic evidence for readers, without counting shared
                # text twice (e.g. SSD inside SSD-based LLM inference).
                result.matches.append(Match(rule["label"], e["term"], e["field"], 0,
                                            rule["topic"], rule["level"], e["context"]))
                continue
            occupied[e["field"]].append((start, end))
            result.matches.append(Match(rule["label"], e["term"], e["field"], e["points"],
                                        rule["topic"], rule["level"], e["context"]))
        result.score = min(int(self.cfg.get("max_score", 60)), sum(m.points for m in result.matches))
        core = any(e["rule"]["level"] == "core" for e in evidence)
        linked = any(e["linked"] for e in evidence)
        if not evidence:
            result.excluded = True
            result.reason = "no_valid_topic_or_context"
        elif not core and not linked:
            result.relevance = "peripheral"
            result.score = min(result.score, int(self.cfg.get("peripheral_score_cap", 6)))
            result.excluded = not self.cfg.get("allow_peripheral", False)
            result.reason = "peripheral_without_storage_context"
        else:
            result.relevance = "core" if core else "related"
            result.reason = "validated_storage_topic"
        return result
