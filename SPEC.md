# Scale — Vision Spec

**Status:** Draft v1
**Date:** 2026-05-10

---

## 1. What Scale Is

Scale is a self-hosted daemon that turns an issue tracker backlog into a continuously running implementation team. It polls for work, dispatches Claude Code agents in isolated workspaces, manages concurrency and retries, and enforces quality gates — so the engineer can focus on what the system should become rather than how to build it.

---

## 2. Foundations

Three principles underpin everything Scale does.

**Workspace isolation.** Every issue gets its own isolated workspace directory. Agent commands run inside that workspace and nowhere else. This is not just a safety measure — it makes runs deterministic and reproducible, ensures agents start from a clean state, and prevents one run from contaminating another. Workspaces persist across attempts so an agent can pick up where it left off; they are removed when an issue reaches a terminal state.

**Resilience.** Scale is designed to keep running without supervision. Transient failures retry with exponential backoff. When an issue changes state and becomes ineligible mid-run, the active session is stopped and released. A crashed or restarted daemon recovers from the issue tracker and filesystem alone — no persistent database is required, because the source of truth is GitHub labels and the workspace directory, not Scale's in-memory state.

**Orchestration boundary.** Scale owns lifecycle management — what runs, when, and whether the output meets the bar. In Scale's implementation this includes label transitions and review comments, which Scale manages directly rather than delegating entirely to the agent. This is a deliberate divergence from the base Symphony model, made because quality gates (triage verdicts, review approvals, merge decisions) require orchestrator-level coordination across multiple issues and runs, not just per-issue agent behavior.

---

## 3. The Operator Model

Scale operates on three tiers.

The **operator** — an individual developer or a team — defines intent: what to build (issues), how agents should work (WORKFLOW.md), and what "good enough" means (REVIEW.md).

**Scale** runs the machine: it decides what gets picked up, when, in what order, and whether the output meets the bar.

The **agent** inside the workspace implements — with no visibility into the larger system.

Scale never makes product decisions. It executes the operator's intent faithfully and stops at the boundary the operator defines. The operator controls how much autonomy Scale has: issues can flow straight to dispatch, require triage assessment first, or be held behind a manual approval gate. The operator is always in control of direction.

---

## 4. The Shift

Scale changes the engineer's relationship to their backlog. A backlog is normally a queue of obligations — work that needs to be done, accumulating faster than it gets cleared. With Scale, it becomes a queue of intentions: things the engineer wants to exist, expressed as issues, that Scale works toward while the engineer thinks at the next level up.

The engineer's job shifts from implementer to architect and reviewer. The question stops being "how do I build this?" and starts being "what should exist, and does this meet the bar?"

This matters most for work that is theoretically within reach but practically not worth doing alone — another API to learn, another framework variation, another well-understood pattern that just needs to be written. Scale lowers the activation energy for that entire category of work.

---

## 5. Quality Throughput

Throughput without quality is just faster debt. Scale's goal is not to maximize the number of issues closed — it's to close them in a way that doesn't leave a wake of technical debt behind. The review subsystem exists for this reason: not as an optional add-on, but as a core quality gate that sits between implementation and merge.

Triage ensures agents spend their turns on well-defined work. Review ensures the output meets the bar before it lands. Conflict resolution keeps branches clean so the queue doesn't stall. Each subsystem is a ratchet against entropy, not a feature.

---

## 6. Self-Hosting as a Core Capability

Scale runs on itself. This repo's issue backlog is Scale's primary proving ground — features are filed as issues, triaged, planned, implemented by Scale agents, reviewed by Scale's reviewer, and merged through Scale's merge queue. Dogfooding isn't incidental; it's how Scale stays honest about whether it actually works.

This has a practical implication: Scale must be capable enough to implement its own next iteration. If a feature is too complex or underspecified for Scale to implement, that's a signal about the feature, the spec, or Scale's capabilities — and worth examining.

---

## 7. Scale as a Project Citizen

When Scale is deployed against a project, it should understand that project — not just execute tickets mechanically. The WORKFLOW.md and REVIEW.md files are the operator's expression of how work should happen in that codebase: what good looks like, what patterns to follow, what the quality bar is. Scale adapts to the project through these files, not the other way around.

Over time, Scale should be capable of improving how it operates within a project — refining its own workflow configuration, identifying gaps in how issues are written, and becoming a better fit for that codebase's conventions. Scale records execution data for every run: token usage, turn counts, success rates, durations. This data should feed back into how Scale operates — surfacing patterns, identifying where agents struggle, and informing how the operator writes better issues and prompts. The goal is a system that gets more useful the longer it runs on a project, not one that stays static.

---

## 8. The WORKFLOW.md Contract

WORKFLOW.md is not configuration managed separately from the codebase — it lives in the repository and versions with the code. The prompt template, runtime settings, quality criteria, and agent behavior are all committed alongside the source. When the codebase evolves, the workflow evolves with it.

This is intentional. The operator's intent — how agents should work, what they should produce, what the quality bar is — belongs in version control, not in a separate service or dashboard. It can be reviewed, diffed, rolled back, and improved through the same process as any other change.

WORKFLOW.md is extensible. The core dispatch behavior is always present; optional subsystems (triage, planning, review, conflict resolution) are enabled by adding the relevant sections. A minimal WORKFLOW.md is a single prompt template and a tracker token. A fully configured one is a complete statement of how an engineering team wants to operate.

---

## 9. Observability

Scale should always make it possible to know what it's doing and why. Every dispatch decision, agent run, review verdict, retry, and merge is logged. The TUI dashboard shows live state. Per-run stats are recorded to `stats.jsonl`. Agent logs capture the full prompt, every streaming event, and the final result.

This isn't just operational convenience — it's a requirement for trust. An operator delegating work to Scale needs to be able to audit any outcome: why an issue was picked up, what the agent did, why a PR was approved or sent back, why a merge failed. Opacity is a bug.

The execution record also feeds Scale's self-improvement loop: patterns in the data — where agents struggle, which issues produce low-quality PRs, where turn limits are hit — are signals the operator can act on.

---

## 10. What Scale Is Not

**Scale is not a product manager.** It does not decide what to build, set priorities, or define done. Those decisions belong to the operator.

**Scale is not a general-purpose workflow engine or distributed job scheduler.** It does one thing well: dispatch coding agents against an issue backlog with quality gates.

**Scale is not a replacement for human judgment.** The operator reviews what Scale produces. The quality bar Scale enforces is the one the operator defined. Scale surfaces information and executes intent — it does not substitute for the engineer's taste, architecture instincts, or product sense.

**Scale is not opinionated about sandboxing.** It runs Claude Code agents with the trust level the operator configures. Operators are responsible for understanding the trust posture of their deployment — who can open issues, what the agent can access, and what approval gates are in place.

**Scale is not inherently tied to any specific issue tracker.** The integration layer is an implementation choice — the underlying model (issues as units of work, labels as lifecycle state, pull requests as output) is tracker-agnostic. GitHub Issues is the reference implementation; other trackers are a natural extension.
