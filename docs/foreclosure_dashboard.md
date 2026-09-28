# Foreclosure dashboard interface

The optional adapter in `renderer/dashboards/foreclosure.js` mounts the foreclosure definition from the injected shared library in interactive mode. Without the library it displays an unavailable notice. See [dashboard authoring](../renderer/dashboards/README.md) for the bounded public dependency.

The caller supplies pipeline data through `window.cc.getPipelineStats(batch)`. Data includes `pipeline_stages` and batch-specific snapshots with `pipeline_summary`; the selected batch stays pinned while polling. Active pipelines poll every 10 seconds; fully complete stages permit the 60-second idle poll.

Gate actions call `window.cc.setBatchGate(batch, gate, setting)`, with `setting` optional. Missing writer support returns an unavailable error. An action receipt and refreshed data determine success; a displayed toggle alone is not proof of a producer write. Producers, databases, credentials and endpoints are supplied by deployment configuration and are outside this repository's dashboard definition.
