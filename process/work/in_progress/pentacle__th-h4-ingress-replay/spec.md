# TH-H4 ingress replay
Base c56afde5eb4c49f6f021f9e67f5691221999a221; branch dot/th-h4-ingress-replay.
TH-v1 rev0.4 H4 is test-only. Deliver ≥40 synthetic class-specific cases, real auth boundaries, caller-only fixture guard, exact state/publication counts and idempotent replay.
Current corpus:53 cases. Confirmed product identity gap remains an ordinary failing regression in test_front_desk_ingress_replay_findings.py. No production repair is authorized in H4; preserve evidence and request fleet/owner disposition while continuing H5.
