# pentacle-chat-core

A small, dependency-free TypeScript library that normalizes coding-agent
provider observations into a common chat-stream event vocabulary. It performs no
I/O: functions take plain objects and return plain objects, so a caller supplies
its own input source, persistence, and transport.

## Install

No published lockfile is required; `npm install` resolves everything (the
library declares no runtime dependencies, so this only fetches dev tooling):

```sh
npm install
```

## Typecheck and test

Everything runs from source against the checked-out tree — there is **no build
step**. The package entry point (`main`/`types`) is `./src/index.ts`; consumers
import the TypeScript source directly.

```sh
npm run typecheck        # tsc --noEmit over src
npm test                 # runs tests/*.test.ts (node --test via tsx) + test typecheck
```

All commands are offline and deterministic; none need a network, a service, or a
production port. Fixtures use synthetic identifiers and example text only.

## Use as a dependency

Vendor this directory into a consumer and reference it with a local `file:`
dependency (for example `"pentacle-chat-core": "file:./pentacle-chat-core"`);
the consumer's bundler compiles the TypeScript source. There is no separate
compiled artifact to publish.
