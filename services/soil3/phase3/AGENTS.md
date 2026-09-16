# Phase3 boundary

Phase3 is the final safety and control boundary. The default for normal feature Issues is **read-only or formal-interface adaptation**. Unless an Issue explicitly authorizes a Phase3 change, do not modify its core irrigation algorithm, hard safety, PendingSoak, stale-sensor protection, cooldown, water-path fault protection, duration cap, prediction breaker, `ActuatorLayer`, or MQTT execution path.

The repository directory is `services/soil3/phase3/`; its Linux production deployment is `/root/water/app/services/soil3/phase3/`. Its state, data, and logs are deliberately separate under `/root/water/runtime/instances/soil3/phase3/`, `/root/water/data/soil3/phase3/`, and `/root/water/logs/soil3/phase3/`. Do not assume JSON/CSV/log files live beside source code.

Do not connect an intelligent Agent to `manual_water` or a direct MQTT publish path. Unless an Issue explicitly authorizes a Phase3 change, this directory may only be read or used through an existing formal adapter/interface. Any authorized change still preserves Phase3 as final authority.
