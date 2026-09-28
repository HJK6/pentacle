# Lead-lock state on dashboards

Lead-lock information is caller-supplied pipeline metadata. A renderer may show ownership and availability only from the current snapshot; absent metadata must remain unknown. The dashboard does not acquire a lock by rendering a label.

Pipeline stages and selected-batch snapshots use the [foreclosure interface](foreclosure_dashboard.md). Writes travel through an explicitly configured action callback and require its success receipt plus refreshed state. Keep producer credentials, lock records and captured operational evidence outside Git. The public dashboard library supplies presentation, not a live lock service.
