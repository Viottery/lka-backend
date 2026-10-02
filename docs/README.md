# Documentation Map

This folder contains the working documentation set for Local Knowledge Agent OS.

## Canonical Documents

- [Current Module Flows](./current_module_flows.md) - current implementation map, foreground/background workflows, and enforced boundaries (2026-10-03).
- [Project Maintenance Review](./project_maintenance_2026-10-03.md) - workspace checkpoint, verified fixes, regression results, and remaining risks.
- [Project Overview](./project_overview.md) - the full mission, vision, architecture, scenarios, and MVP scope.
- [Backend Engineering Guide](./backend_engineering_guide.md) - the backend-oriented architecture and module breakdown.
- [Backend Implementation Plan](./backend_implementation_plan.md) - phased delivery plan mapped to the project vision.
- [Cross-Platform Support](./platform_support.md) - native Windows/Linux backend support, path handling, and test matrix.
- [HTTP API Contract](./api_contract.md) - the current HTTP surface and response shapes.
- [AI Coding Standard](./ai_coding_standard.md) - the execution and reporting contract for coding work.
- [MVP Todolist](./mvp_todolist.md) - the step-by-step implementation checklist for the MVP.
- [LangGraph Core Migration Plan](./langgraph_core_migration_plan.md) - executable plan for moving Agent orchestration to LangGraph while preserving project-owned semantics.
- [Personal Knowledge And Agent Memory Plan](./personal_knowledge_memory_plan.md) - local-first knowledge base, memory layers, and phased retrieval roadmap.
- [Memory And Background Development TODO](./memory_background_todolist.md) - staged implementation, edge cases, tests, and release gates for durable memory and asynchronous work.
- [Memory And Background Implementation](./memory_background_implementation.md) - implemented contracts, configuration, API, tests, and remaining release gates.
- [Memory And Background Development Report](./memory_background_development_report.md) - scenario failures and fixes, measured results, rollout limits, and remaining provider/production gates.
- [Laya Auxiliary Routing Guide](./laya_auxiliary_routing_guide.md) - isolated findings and decision boundaries for using Laya as an optional routing aid.

## Source Draft

- `../temp.md` is the original draft that informed the project overview.

## Recommended Reading Order

Start with Current Module Flows for the implemented system; use the following
documents for the project vision, contracts, and delivery queue.

1. Project Overview
2. Backend Engineering Guide
3. Backend Implementation Plan
4. Cross-Platform Support
5. HTTP API Contract
6. AI Coding Standard
7. MVP Todolist
8. LangGraph Core Migration Plan
9. Personal Knowledge And Agent Memory Plan
10. Memory And Background Development TODO
