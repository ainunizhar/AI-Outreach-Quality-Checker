"""Outreach quality checker: rule checks for a batch of AI-written sales emails.

A clean re-implementation of the checks used in the original campaigns (those scripts hold campaign data, so they
stay private). Thresholds match the originals.

    python outreach_check.py sample_batch.json

Exit code 0 = every email passed, 1 = at least one problem (each problem is printed).
Batch file: {"sender": "...", "signature": "...", "emails": [{"id", "first_name", "company", "subject", "body"}]}
"""
import json
import re
import sys
from collections import Counter, defaultdict

BODY_WORDS = (90, 175)        # words in the body, greeting and signature excluded
SUBJECT_MAX = 62              # characters
MAX_QUESTIONS = 2
PHRASE_N = range(4, 8)        # 4- to 7-word phrases ...
PHRASE_CAP = 3                # ... may appear in at most 3 emails of a batch
OPENING_CHARS, OPENING_CAP = 46, 3
STOPWORDS = set("a an and are as at be by for from has have i in is it its of on or our that the this to we with "
                "you your".split())

# Patterns that make AI-written outreach generic, pushy or unsupported. Each one is a "family" of phrasings.
BANNED = {
    "claim about the reader's security": r"\byour (security|defen[cs]es?|setup) (is|are|looks?) (weak|outdated|"
                                          r"lacking|exposed)|\byou(?:'re| are) (exposed|vulnerable|at risk)\b",
    "hints at an incident": r"\b(recent|your) (breach|incident|attack|leak)\b|\bafter what happened\b",
    "statistic (needs a source)": r"\d+(\.\d+)?\s?%|\bper ?cent\b|\b\d+x\b",
    "guarantee": r"\bguarantee[sd]?\b|\b(fully|completely|100%) (secure|protected)\b",
    "stacked ask": r"\b(call|chat|meeting)\b[^.?!]{0,60}\b(and|or|plus)\b[^.?!]{0,30}\b(demo|deck|brochure|trial)\b",
    "stiff opener": r"\bI hope this (email|message) finds you\b|\bI am writing to\b",
    "long dash": "[" + chr(0x2013) + chr(0x2014) + "]",   # en and em dash
}
SENDER_TOKEN = "zzsenderzz"
PLACEHOLDER = re.compile(r"\{\{.*?\}\}|\[[A-Z][A-Z _]+\]|<[A-Z_]+>")


def words(text):
    return re.findall(r"[a-z0-9']+", text.lower())


def core(body, sig):
    """Body without the greeting line and the fixed signature: the part the LLM wrote."""
    lines = body.strip().splitlines()
    text = "\n".join(lines[1:]) if lines and re.match(r"^(hi|hello|dear)\b", lines[0], re.I) else body
    return text.replace(sig.strip(), "").strip()


def check(batch):
    sig, emails = batch["signature"], batch["emails"]
    problems = defaultdict(list)
    for e in emails:
        p, body, text = problems[e["id"]], e["body"], core(e["body"], sig)
        n = len(words(text))
        if not BODY_WORDS[0] <= n <= BODY_WORDS[1]:
            p.append(f"length {n} words (allowed {BODY_WORDS[0]}-{BODY_WORDS[1]})")
        if not e.get("subject"):
            p.append("no subject line")
        elif len(e["subject"]) > SUBJECT_MAX:
            p.append(f"subject {len(e['subject'])} characters (max {SUBJECT_MAX})")
        if not re.match(rf"^(Hi|Hello|Dear) {re.escape(e['first_name'])},", body.strip()):
            p.append(f"greeting does not use the contact's first name ({e['first_name']})")
        if e["company"].lower() not in body.lower():
            p.append(f"company name ({e['company']}) not in the email")
        if sig.strip() not in body:
            p.append("signature missing or changed")
        if PLACEHOLDER.search(body):
            p.append(f"unfilled placeholder {PLACEHOLDER.search(body).group(0)}")
        if text.count("?") > MAX_QUESTIONS:
            p.append(f"{text.count('?')} questions (max {MAX_QUESTIONS})")
        for family, rx in BANNED.items():
            m = re.search(rx, text, re.I)
            if m:
                p.append(f"{family}: \"{m.group(0)}\"")

    # batch rules: the same LLM tends to reuse the same phrases, openings and subjects across a batch
    for label, values in (("subject", [e["subject"] for e in emails]), ("contact", [e["id"] for e in emails]),
                          ("company", [e["company"] for e in emails])):
        for v, c in Counter(values).items():
            if v and c > 1:
                problems["batch"].append(f"{label} used {c} times: \"{v}\"")
    openings = Counter(core(e["body"], sig)[:OPENING_CHARS].lower() for e in emails)
    for o, c in openings.items():
        if c > OPENING_CAP:
            problems["batch"].append(f"opening used by {c} emails (max {OPENING_CAP}): \"{o}...\"")
    seen = defaultdict(set)
    sender = re.compile(re.escape(batch.get("sender", "")), re.I) if batch.get("sender") else None

    def phrase_words(e):   # the sender's own name is exempt, as in the originals (domain terms do not count)
        text = core(e["body"], sig)
        return words(sender.sub(f" {SENDER_TOKEN} ", text) if sender else text)

    for e in emails:
        w = phrase_words(e)
        for n in PHRASE_N:
            for i in range(len(w) - n + 1):
                g = tuple(w[i:i + n])
                if SENDER_TOKEN not in g and sum(t not in STOPWORDS for t in g) >= 2:
                    seen[g].add(e["id"])
    # over-used phrases, merged into whole repeated passages (one line per passage, not one per overlapping piece)
    groups = defaultdict(set)
    for g, ids in seen.items():
        if len(ids) > PHRASE_CAP:
            groups[frozenset(ids)].add(g)
    for ids, grams in groups.items():
        w = phrase_words(next(e for e in emails if e["id"] in ids))
        covered = [False] * len(w)
        for n in PHRASE_N:
            for i in range(len(w) - n + 1):
                if tuple(w[i:i + n]) in grams:
                    covered[i:i + n] = [True] * n
        for i in range(1, len(w) - 1):   # bridge over the sender's name inside a repeated passage
            covered[i] = covered[i] or (w[i] == SENDER_TOKEN and covered[i - 1] and covered[i + 1])
        i = 0
        while i < len(w):
            if covered[i]:
                j = i
                while j < len(w) and covered[j]:
                    j += 1
                passage = " ".join(w[i:j]).replace(SENDER_TOKEN, batch.get("sender", "").lower())
                problems["batch"].append(f"phrase in {len(ids)} emails (max {PHRASE_CAP}): \"{passage}\"")
                i = j
            else:
                i += 1
    return {k: v for k, v in problems.items() if v}


def main(path):
    batch = json.load(open(path, encoding="utf-8"))
    problems = check(batch)
    n_bad = sum(1 for k in problems if k != "batch")
    for key, items in problems.items():
        print(f"\n{key}")
        for it in items:
            print(f"  FAIL  {it}")
    print(f"\n{len(batch['emails'])} emails checked: {len(batch['emails']) - n_bad} passed, {n_bad} failed, "
          f"{len(problems.get('batch', []))} batch problems")
    print("RESULT:", "OK to export" if not problems else "NOTHING EXPORTED - fix the copy and run again")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "sample_batch.json"))
