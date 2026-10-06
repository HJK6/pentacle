# TH-H3 runtime directory
Base: c56afde5eb4c49f6f021f9e67f5691221999a221. Branch: dot/th-h3-runtime-directory.
TH-v1 rev 0.4 H3: narrowly change runtime marker path resolution, preserve default path and serialized bytes. PENTACLE_RUNTIME_DIR unset/empty keeps ~/.pentacle; nonempty paths use normal Node path semantics, including relative paths relative to current working directory.
One production writer and no shipped reader exist at this base; the synthetic marker fixture reads using the shared resolver. Existing parent sentinel remains an independent legacy-path oracle.
Gate runtime is independently disposable even with --keep, covers initial in-process host and restarts, and restores caller environment. Root runner pins runtime into its owned test home.
No full Chrome/daemon E2E execution by dot; fleet owns it.
