---
name: cwpilot:insights
description: Capture, browse, relate, score, and merge project insights.
---

Load when recording or looking up project learning.

## Insights — what the project has learned

Work items say what is to be done. **Insights** say what has been learned: a decision and its
reasoning, a pattern, a risk, a tradeoff, an assumption worth remembering. They accumulate
across sessions, so a later one can find out why something is the way it is without asking.

Insights are grouped into **domains** — architecture, security, performance, ux, whatever the
project actually talks about — and linked, both to each other (this supports that, this
contradicts that, this refines that) and to the work and decisions they came from. An insight
carries a **confidence** that moves as evidence accumulates: corroboration raises it,
contradiction lowers it, and near-duplicates merge rather than pile up.

**Capture** when a decision gets made, a pattern shows itself, a risk or tradeoff is named, or
an assumption is questioned — from the user's words or from your own reasoning. Capture the
meaning, not the transcript: a type, a short label, one tight sentence.

```bash
printf 'decision|JWT + Redis for auth|Stateless tokens with a Redis blacklist for revocation|\n' \
  | cwpilot insight add architecture --domain architecture
```

Each line is `type|label|description|edges`; the types are fact, pattern, principle, tradeoff,
assumption, question, decision, risk, opportunity. Ids, timestamps, confidence and dedup are
the CLI's job — never hand-author them.

**Look** before deciding something the project may already have decided, and when picking up
work whose context you do not hold:

```bash
cwpilot insight browse --anchor <work-id, file path, or a phrase>
cwpilot insight relevant --work <work-id>
```

`browse` walks outward from any handle you happen to hold; `relevant` answers the narrower
question of what bears on one work item. `cwpilot insight --help` has the rest — linking two
insights, scoring a decision, reporting contradictions, and corpus maintenance.

Extract by understanding, not keyword match: "JWT avoids session storage" and "stateless
tokens" are one insight. Say *why* two insights connect. Raise confidence only on real
evidence, and capture your own reasoning too, not just the user's.
## Insight lifecycle

Capture → Relate → Score → Supersede

1. Capture — learning is recorded (fact, pattern, principle, tradeoff, assumption, decision, risk)
```bash
cwpilot insight add architecture \
  --domain architecture \
  --type decision \
  --label "JWT + Redis" \
  --description "Stateless tokens with Redis blacklist"
```
2. Relate — insight is linked to other insights (supports, contradicts, refines) and to originating work
```bash
cwpilot insight link domain1:insight1 domain2:insight2 contradicts
```
3. Score — confidence is assigned based on evidence; surfaces in relevance queries
```bash
cwpilot insight relevant --work <work_id> -k 10
```
4. Supersede — insight is marked as replaced by another; original preserved for lineage
```bash
cwpilot insight merge <target> --merge-from <source>
```
