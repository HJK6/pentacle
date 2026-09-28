# Owned web-mode browser gate

From the public checkout, run `npm run build:web` and `node test/e2e/web_gate.js`. The gate requires Node dependencies, Python daemon dependencies, Chrome/Chromium, tmux and zsh. `--python` selects an explicitly provisioned Python environment. The default gate seeds disposable sessions and transcripts, starts an owned loopback daemon and web host, then drives the served page over CDP.

The gate reports named assertions and cleans its own browser, daemon, listeners, fixtures and tmux sessions. It never requires an installed phone or native application build. Inspect the actual failed assertion before classifying a failure as product or harness; a process or build readback alone is not functional proof.

Renderer contract cases use fresh raw frames and fake send/cancel bridges through real controls. They prove renderer ordering, duplicate suppression, stop retry states and returned draft presentation. They do not prove transport delivery, a steering ledger, reconnect semantics or silent half-open recovery. Separate existing public replay/paging/reconnect tests cover their named contracts. Browser state and bridges are restored by reload around isolated cases.

Use the default owned gate for automation. External `--profile` configurations are operator-provided live inputs and require separate resource authority. Keep raw gate receipts outside shared memory and store only location/hash metadata in the work item.
