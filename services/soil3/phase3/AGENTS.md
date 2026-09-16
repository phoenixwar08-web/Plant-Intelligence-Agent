# Phase3 boundary

Phase3 is the final safety and control boundary. The default for normal feature Issues is **read-only or formal-interface adaptation**. Unless an Issue explicitly authorizes a Phase3 change, do not modify its core irrigation algorithm, hard safety, PendingSoak, stale-sensor protection, cooldown, water-path fault protection, duration cap, prediction breaker, `ActuatorLayer`, or MQTT execution path.

Do not connect an intelligent Agent to `manual_water` or a direct MQTT publish path. Unless an Issue explicitly authorizes a Phase3 change, this directory may only be read or used through an existing formal adapter/interface. Any authorized change still preserves Phase3 as final authority.
