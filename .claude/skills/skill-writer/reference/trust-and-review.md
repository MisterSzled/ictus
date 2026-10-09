# Trust and review

For reviewing or installing a skill somebody else wrote, and for deciding what
capability to grant one. Not needed while writing your own.

The tier and gate model below is the authors' **proposal**, which they describe
as *"a governance proposal rather than an empirically validated system"* whose
value "is to make the trust assumptions explicit and testable" (§6.4). The
numbers motivating it are measured; the response to them is not.

## Why it is needed

Liu et al. collected 42,447 skills from two marketplaces and analysed 31,132
(§6.2):

- **26.1% carry at least one vulnerability**, across 14 patterns in four
  categories: prompt injection, data exfiltration (13.3%), privilege escalation
  (11.8%), supply chain.
- **OR=2.12, p<0.001** for skills that bundle an executable script.
- **5.2% show high-severity patterns** suggesting malicious intent.

A second study behaviourally verified 98,380 skills and confirmed 157 malicious
ones carrying 632 vulnerabilities — averaging **4.03 vulnerabilities across 3
kill-chain phases**, which is why static analysis alone is not enough (§6.3).

Two archetypes: **Data Thieves** (exfiltrate credentials via supply-chain
technique) and **Agent Hijackers** (subvert decisions through instruction
manipulation). **One industrialised actor accounted for 54.1% of confirmed cases
through templated brand impersonation** — so a skill that resembles a known
vendor's is the single strongest prior for trouble.

Schmotz et al. separately showed malicious instructions in SKILL.md or in
referenced scripts can exfiltrate internal files and passwords, and that a
benign task-specific approval with "Don't ask again" **carries over to closely
related but harmful actions** (§6.1) — the rule `check_skill.py` enforces.

## Four gates, in order

| | Gate | Catches |
| --- | --- | --- |
| G1 | static analysis — pattern matching, dependency scan | known vulnerability signatures |
| G2 | LLM semantic classification: actual instructions vs **declared purpose** | indirect prompt injection |
| G3 | behavioural sandbox | side effects invisible to static analysis |
| G4 | permission manifest vs what G3 observed | capability the skill did not declare |

Only G2 catches intent mismatch. Only G3 sees side effects. Failing any gate
means reject or quarantine.

## Four tiers

| Tier | Provenance | Permissions | Gates |
| --- | --- | --- | --- |
| T1 | unvetted / sandboxed | instructions only, full tool isolation | G1 |
| T2 | community-reviewed | read-only tools, **no code execution**, user confirm | G1+G2 |
| T3 | organization-vetted | declared tools only, scoped file access, code execution, no network | G1+G2+G3 + admin |
| T4 | vendor-certified | full access, code execution, network I/O | G1–G4 + vendor review |

The tiers map onto the three disclosure levels: **L1 metadata is exposed at T1,
L2 instructions at T2+, L3 executable scripts require T3 or T4** — never T1 or
T2, which is the direct response to the 2.12× finding. Governance decisions
therefore track the actual attack surface rather than being applied uniformly.

## Trust is not permanent

Deployed skills would be subject to continuous runtime monitoring. Anomalous
behaviour — unexpected tool calls, permission boundary probes — triggers
demotion or revocation; a clean history permits promotion. **Re-review when the
environment changes, not only at installation.**
