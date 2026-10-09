# Open questions

What the field has not settled. Read this before asserting a rule about skills
that is in neither "Do this" nor "Yours to judge" in SKILL.md — the answer may
be that nobody knows.

## Named unsolved (§7, §8)

**Skill selection at scale.** Li (2026) found a *phase transition*: beyond a
critical library size, selection accuracy degrades sharply. The paper concludes
there are "fundamental limits on how many skills a single agent can effectively
manage" — but names no number. Tool Search addresses it partially. `§4.6, §7C2`

**Capability-based permissions.** Execution runs on implicit trust: once loaded,
a skill can direct the agent to use any available tool. A model where each skill
declares required permissions and the user grants them explicitly "would
significantly reduce the attack surface" — it does not yet exist. `§7C4`

**Verification and testing.** Confirming a skill does what it claims *and
nothing more* is "an open technical problem that intersects AI safety and formal
methods". Skills have no standardised testing frameworks. Assume a review missed
something. `§7C5`

**Composition and orchestration.** Conflict resolution, resource sharing and
failure recovery across multiple skills "remain underdeveloped". Composition
graphs and dynamic composition are called initial solutions. `§7C3`

**Catastrophic forgetting.** Whether dynamically loaded skills can "overwrite"
useful default model behaviours is "poorly understood". This is the paper's only
support for the idea that skills can make a model *worse*, and it is flagged as
unknown rather than measured. `§7C6`

**Cross-platform portability.** Skills authored for one platform may implicitly
depend on its code execution environment, tool signatures and model behaviours.
True portability needs either a universal runtime or skill compilation per
platform. `§7C1`

**Evaluation methodology.** Benchmarks assess task completion and rarely assess
skill quality. Three metrics are proposed and do not exist: *reusability* (does
it generalise across tasks?), *composability* (can it combine with others?),
*maintainability* (how robust to environmental change?). `§7C7`

**Learned skills are not artifacts.** SAGE and SEAgent show agents can acquire
skills from experience, but those are model-internal — they "cannot be
inspected, shared, or governed the way human-authored SKILL.md files can".
Externalising them is named a research direction. `§8`

## Four claims this paper does not make

Commonly attributed to it, and absent from it. Source them elsewhere or drop
them:

- **No adherence ceiling.** No figure for how reliably models follow long
  instruction sets.
- **No primacy or recency finding.** Nothing about early instructions being
  retained better than late ones. Ordering advice is not in this paper.
- **No claim that stronger models benefit less from skills.** §7C6 raises
  interference with base capabilities as an open question; it neither measures
  it nor relates it to model strength.
- **No per-step correctness arithmetic.** The compounding-error argument for
  short chains is not made here.

And the tier/gate framework in [trust-and-review.md](trust-and-review.md) is
explicitly a proposal, not a validated system.
