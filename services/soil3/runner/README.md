# soil3 Strategy Runner V1 (dry-run)

This module persists and advances the `water`, `wait`, `observe`, and `stop`
steps in a `strategy.v1` proposal. It is deliberately dry-run only: water steps
create an atomic JSON completion record with `physical_action_performed: false`.
The module does not import or call Phase3, MQTT, `manual_water`, Episode, or Gate.

Every strategy has one UUID-named state file beneath the caller-provided store
directory. Completed steps and the current index are written atomically. Waits
store an absolute UTC `wait_until`; a new process can call `resume` before or
after that instant. Re-submitting the same strategy is idempotent, while reusing
its id for different content is rejected.

```powershell
python -m services.soil3.runner.service --store-dir runtime/runner start --strategy strategy.json
python -m services.soil3.runner.service --store-dir runtime/runner resume --strategy-id <uuid>
python -m services.soil3.runner.service --store-dir runtime/runner read --strategy-id <uuid>
```

No production directory default is provided. This first version assumes a
single writer per strategy state file.
