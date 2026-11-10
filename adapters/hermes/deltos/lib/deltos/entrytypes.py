"""What kind of statement an entry is, read from its label and value: one answer
every part of memory uses.

  state     what a thing is like now, and it drifts (status, host, port, version,
            a path, a config key): checks, "not confirmed in N weeks", due lists
  identity  what a thing is and whose it is (owner, relationship, kind, email):
            stable; changed by a correction or by news, never doubted by age
  decision  what someone chose for a project or an offer (price, timeline, scope,
            hardware recommendation): stands until a later decision
  rule      how to act, standing (never / always / must, a policy, a convention):
            shown whenever its thing comes up, whatever the question's words
  event     something that happened once (a launch day, "shipped v2"): dated,
            never the current state of anything

Nothing is stored for a type: it is read from the entry each time, so every
writer's entries have one and a better reading changes them all. The store's
refusals (store.value_problem: secrets, one-time codes, run counts as settings,
events as a status) are the sixth kind: what is never kept.

11-06 audit of the live store: decisions and rules were 3% of 2,407 entries and
none of them reached a block unless the question named their label; a client's
"never touch real patient data" was one of them.
"""
from __future__ import annotations

import re

# Labels whose value drifts in live state: doubt ('not confirmed in N weeks') and checks.
VOLATILE = {"status", "version", "latest release", "model", "host", "port", "service", "url",
            "container", "branch", "ip"}


def drifts(label: str) -> bool:
    """A label whose value drifts in live state (settings are chosen, but a setting
    with a known check is due like any other: deltos/checks.py)."""
    lab = label.lower().strip()
    return not lab.startswith("setting ") and (lab in VOLATILE or lab.rsplit(" ", 1)[-1] in VOLATILE)

TYPES = ("state", "identity", "decision", "rule", "event")

# A label that names a standing rule: how to act.
# Not "preference": a person's tastes ("food preference: vegan") are facts about them, not how
# to act; pinned as rules they grew LongMemEval blocks 15% (gate, 11-06).
RULE_LABEL = re.compile(r"(?i)\b(?:rule|rules|policy|policies|convention|conventions|guideline|guidelines|"
                        r"never|always|must|dont)\b")
# A value that is one (what to do or not do), on a label that does not drift.
RULE_VALUE = re.compile(r"(?i)^(?:never|always|don['’]?t|do not|must)\b")
# A label for what was chosen: for a project, an offer, a client.
DECISION_LABEL = re.compile(r"(?i)\b(?:decision|decisions|decided|choice|chosen|plan|scope|price|prices|pricing|fee|"
                            r"fees|quote|timeline|deadline|budget|terms|recommendation|approach|strategy|"
                            r"out of scope|in scope|deliverables?)\b")
# A thing whose price or release is a fact about it, not a choice (a product, a model).
PRODUCT_KINDS = re.compile(r"(?i)\b(?:product|model|machine|hardware|device|gpu|chip|computer|desktop|laptop|"
                           r"phone|server|asset|watch|car|vehicle)\b")
# Something that happened once: a label for when, or a status written as what happened.
EVENT_LABEL = re.compile(r"(?i)(?:^|\s)(?:date|day|launched|shipped|sent|released on|happened|incident|"
                         r"started on|created on|merged on|filed on|last run)$")
EVENT_STATUS = re.compile(r"(?i)^(?:shipped|merged|pushed|sent|restarted|fixed|rebuilt|migrated|reverted|filed|"
                          r"answered|flagged|found|added|bumped|tagged|replied|emailed|posted|patched|ran|"
                          r"graded|wrote|drafted)\b")
DATE = re.compile(r"^\d{4}-\d\d-\d\d$")
# Labels that say what a thing is or whose.
IDENTITY_LABEL = re.compile(r"(?i)^(?:kind|owner|relationship|son|daughter|mother|father|parent|partner|client|"
                            r"business|email|phone|birthday|name|full name|license|language|tech|stack|"
                            r"part of|made by|vendor|maker|purpose|role|job|employer|company|title|type)$")
# Labels that drift even when not in the checks' list: where things are and how they're set.
STATE_WORDS = re.compile(r"(?i)\b(?:location|path|address|ip|endpoint|url|domain|port|host|version|status|model|"
                         r"service|container|branch|release|config|count|size|state|backend|mode|level|"
                         r"schedule|timer|cron|enabled|disabled|limit|cap|memory|disk|gpu|repo)\b")


def entry_type(label: str, value: str = "", kind: str = "") -> str:
    """The type of an entry (TYPES), from its label, its value and its thing's kind."""
    lab = label.strip()
    low = lab.lower()
    plain = low.removeprefix("setting ").strip()
    v = value.strip()
    if low == "kind" or IDENTITY_LABEL.match(plain):
        return "identity"
    if RULE_LABEL.search(plain) or (RULE_VALUE.match(v) and not drifts(lab)):
        return "rule"
    if low == "status" and EVENT_STATUS.match(v):
        return "event"
    if DECISION_LABEL.search(plain) and not PRODUCT_KINDS.search(kind or ""):
        return "decision"
    if EVENT_LABEL.search(plain) or (DATE.match(v) and not drifts(lab)):
        return "event"
    if low.startswith("setting ") or drifts(lab) or STATE_WORDS.search(plain):
        return "state"
    return "identity"
