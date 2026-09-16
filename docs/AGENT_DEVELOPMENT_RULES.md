# AI / Agent development rules

## Required flow

`Read AGENTS.md and Issue → read only routed docs and affected code → confirm boundaries → minimal implementation → test → inspect diff → submit Review`

The root [AGENTS.md](../AGENTS.md) routes the task to its relevant one or two documents. Read a local `AGENTS.md` whenever the target directory has one.

## Non-negotiable limits

First solve the explicit problem and reuse existing capability. Add a new module only when the authorized work cannot fit the current structure.

Do not perform unrelated refactors, large file moves, bulk renames, protocol changes, framework adoption, production-configuration edits, database or historical-data edits, or speculative empty-module scaffolding. Do not change a stable working module merely to make it “cleaner” or “more advanced”.

If an Issue-external problem is found, record it and leave it unchanged unless it directly blocks the Issue. If it blocks the Issue, explain the smallest necessary fix in the Review handoff.

## Review handoff

Every implementation remains in **Review** after its own tests pass. The handoff must state: (1) what changed, (2) what was not changed, (3) affected modules, (4) actual verification, and (5) remaining issues or risks. The daily owner decides Done only after checking behavior, tests, diff scope, protocol compatibility, integration of all three daily Issues, and Phase3/MQTT/production-data boundaries.
