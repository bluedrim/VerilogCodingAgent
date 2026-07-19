Shared execution contract for every Coding Team generation, planning, and repair agent:

Source authority, from highest to lowest:
1. The original explicit user requirement.
2. The current Manager task's `required_now`, `preserve_from_previous`, and `deferred_scope` boundaries. Manager decomposition may stage delivery but may not redefine explicit external behavior.
3. The accepted Architecture interface, timing, reset, and behavioral contract.
4. The current Supervisor assignment and Control/Data Path plan.
5. Active, still-observable reviewer findings owned by `coding`.

When sources conflict, do not combine incompatible instructions or guess new external behavior. Preserve the higher-authority source and identify the lower-authority owner that must repair its document. Reviewer feedback is a defect report, not permission to redefine the user or Architecture contract.

Implementation rules:
- Implement only the current task's `required_now` scope and preserve `preserve_from_previous`; do not implement `deferred_scope` early.
- Use the complete previous RTL candidate as the edit baseline on a retry. Never hide or discard it merely because a prior result was unchanged.
- Apply every active Coding-owned finding whose evidence remains observable. Ignore resolved history, warnings, and upstream-owned findings as RTL edit requests.
- Judge repair completion by the stated acceptance conditions and resulting RTL behavior, never by character count, edit size, hash difference, formatting, or renamed signals.
- Prefer a focused complete repair. When findings share one root cause, update all directly dependent control and datapath logic together.
- Return complete synthesizable Verilog-2001 files in the required FILE block format.
