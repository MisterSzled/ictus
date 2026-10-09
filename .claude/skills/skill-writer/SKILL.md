---
name: skill-writer
description: Create, shrink or review an agent skill — what belongs in SKILL.md versus a reference file, how to word the description so it routes, and what bundling a script costs. Ships a checker.
---

# Writing a skill

A skill is three levels with three different prices. **L1** is the frontmatter,
pre-loaded into the system prompt for *every installed skill* whether it fires
or not — you pay for it on every request, forever. **L2** is this body, loaded
only when the skill triggers. **L3** is every other bundled file, free until
L2 names it. Nearly every rule below falls out of that. `§3.1`

## Do this

1. **One directory, named for the skill.** `SKILL.md` plus `reference/` for L3.
2. **Write L1 first.** It is the only part always paid and the only part that
   decides whether the skill ever runs.
3. **Put in L2 only what is needed every time the skill fires.** Everything else
   goes to `reference/`, linked from L2 with a line saying *when* to read it.
4. **Run the checker and fix what it reports:**

   ```sh
   python3 .claude/skills/skill-writer/check_skill.py <skill-dir>     # one skill
   python3 .claude/skills/skill-writer/check_skill.py .claude/skills  # the library, and its total L1 cost
   ```

5. **Answer the judgement list below.** The checker cannot.

**Done = the checker exits 0 and you have answered every question in
"Yours to judge".** Nothing else is an exit condition.

## The checker owns these — do not restate them in prose

Frontmatter present, `name` kebab-case and matching the directory, L1 and L2
token budgets, L3 files that are linked but missing, L3 files that are bundled
but never named, bundled scripts, and any request for blanket approval. When one
fails it explains itself; a rule that a script enforces does not also belong in
a skill. `§3.1 §6.1 §6.2`

## Yours to judge

- **Is the `description` router input rather than a summary?** It is what the
  router matches on. Write the situations it covers in the words somebody would
  use. Selection accuracy degrades past a critical library size, and this is the
  only lever one skill has against that. `arch §3.1` `measured §4.6`
- **Does it change what the agent can *do*?** A skill injects instructions and
  modifies the execution context — tools, permissions. A tool returns a result;
  a skill prepares. If yours only states facts, it is documentation. `arch §3.2`
- **Is any connectivity in it?** Skills say *what to do*; MCP says *how to
  connect*. Naming a server and how to read its output is fine; being the
  connection is not. `arch §3.3`
- **Could a script replace this prose?** Prefer code execution to structured
  tool calls — 79.5%→88.1% accuracy, up to 85% less token overhead — and prefer
  pointing at something runnable over stating a fact that will drift. Stale
  instructions are the commonest benign failure. `measured §3.4` `§8`
- **If it bundles a script, does that script earn its place?** 26.1% of 31,132
  community skills carry a vulnerability; bundling scripts makes it 2.12×
  likelier (p<0.001), and needs trust tier T3+. `measured §6.2`
- **Does it declare what it needs** — tools, paths, network? Nothing enforces a
  permission manifest; today's model is implicit trust. `proposed §6.4`
- **Does it name its platform dependencies?** Portability is aspirational.
  `open §7C1`
- **Have you written the negative tests?** Hallucinated procedures, cascading
  failures, deadlock with another skill, silent permission escalation,
  adversarial chaining. No standard framework exists; write them anyway.
  `open §7C5`
- **Have you tested the combinations you intend?** Multi-skill composition does
  not work by assumption. `open §7C3`

Tags mark what a rule rests on: `measured` has a number, `arch` is how the
runtime works, `proposed` is unvalidated, `open` is unsolved. Keep them — a
loaded skill's instructions are authoritative context, so a proposal written
down as a rule gets followed without question. `§6.1`

## When you need more

- **[reference/trust-and-review.md](reference/trust-and-review.md)** — reviewing
  or installing somebody else's skill, or deciding what capability to grant one.
- **[reference/open-questions.md](reference/open-questions.md)** — what the
  field has not settled, and four claims commonly attributed to the source paper
  that it does not make.

Source: Xu & Yan, *Agent Skills for LLMs* (arXiv:2602.12430v4, AgentSkills '26).
Section numbers above refer to it.
