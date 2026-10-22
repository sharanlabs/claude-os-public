# Build AI products with slash commands and specialists

claude-os is a development workflow around Claude Code. The full configured system combines a command entry point, a planning council, task-specific specialists, execution, verification, and review. The public evaluator is one component of that workflow, rather than the project's entire purpose.

## Three commands, three responsibilities

| Command in the full configured system | Responsibility | Boundary |
|---|---|---|
| `/claude-os <idea>` | Inspect a new or existing project, clarify the idea, select resources, and settle an approved goal and plan | The deciding phase includes the owner; an unsettled idea is not sent straight to execution. |
| `/autopilot` | Drive an approved goal through the development stages, coordinating work and verification | It does not authorize publishing, deploying, or other external actions by itself. |
| `/enhance` | Assess an existing project's goal and propose ranked improvements | It proposes; the owner chooses what proceeds to planning and implementation. |

These are skill-driven agent workflows, not ordinary shell executables. They require the complete skill definitions, specialist agents, project configuration, and compatible host runtime. **The current public clone does not install these three commands or the full specialist roster.**

## Example: an AI support assistant

An illustrative full-system request is:

```text
/claude-os Build an AI support assistant that cites approved documentation and escalates uncertain answers.
```

The planning council can examine who needs the product, what it must do, whether AI is necessary, and what the smallest credible version should include. The owner settles the direction before execution.

The project advisor selects a working set instead of assigning every agent to every task:

| Role | Example contribution |
|---|---|
| Idea, user-need, and feasibility council | Sharpen the problem, challenge demand assumptions, and scope a credible build. |
| AI engineering specialist | Retrieval, prompts, agent/tool behavior, and relevant AI failure modes. |
| Frontend specialist | User interface, interaction states, and accessibility. |
| Backend specialist | API contracts, services, data flow, and authentication design. |
| Evaluation specialist | Test cases, scoring criteria, regressions, and evidence of behavior. |
| Security specialist | Threat review and findings for the builder to address. |
| Separate acceptance reviewer | Assess the artifact and returned verification evidence. |

The main session keeps the build coherent and delegates bounded work. Specialists are selected for relevance; this is not a claim that all roles always run or that a list of agents guarantees quality.

The full pipeline covers brainstorm, research, plan, resources, execution, and authorized release. After the goal is approved, `/autopilot` continues the applicable remaining stages, coordinating implementation and verification. Failed checks route work back for correction. Progress and decisions are recorded for resumption. A separate review and actual runtime evidence remain necessary; model agreement alone does not prove correctness.

## What is available in this repository

The public edition includes the Python evaluator, task linter, synthetic examples, and three attributed planning/review skills. Those skills can help question and critique a plan, but they are not the complete product-building command engine.

The [walkthrough](WALKTHROUGH.md) is runnable without an API key. It demonstrates how to check supplied outputs and structured review verdicts. It does not demonstrate an end-to-end product build, invoke a specialist team, or measure model review accuracy.

## Evidence and next work

This explanation was checked against the local command definitions for `claude-os`, `autopilot`, and `enhance`, the project-advisor definition, the pipeline contract, and the named specialist definitions on 2026-10-22. These source definitions describe the full system's behavior; no new full-system build was executed for this documentation update.

Publishing a portable command engine requires reviewing its dependencies, configuration, licensing, installation, and end-to-end tests. That work is separate from the current supported evaluation toolkit. See the [roadmap](ROADMAP.md).
