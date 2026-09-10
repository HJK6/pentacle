# Cosmic visual regression

Visual regression tests for the desktop chat surface use only deterministic fixtures. They do not connect to a live daemon, install a package, or read another application.

## Fixture matrix

`test/cosmic_scene_builder.ts` is the shared source for the structural test
(`test/cosmic_visual.test.ts`) and HTML gallery generator
(`test/generate_cosmic_chat_states.js`). Its `STATES` are:

1. `empty`;
2. `populated`;
3. `working`;
4. `question_single`;
5. `question_multiselect`;
6. `markdown_code`; and
7. `markdown_table`.

The builder supplies synthetic `mage:claude-mage-cosmic` stream identities,
fixed timestamps, bounded text, and production renderer components. These are
TypeScript fixtures, not separate JSON files. Production sigil order is
`djinni`, `sun`, `mage`, `flower`; it does not define an infrastructure host roster.


## Assertions

Assert layout roles, labels, visible row counts, option labels, status text, and the content digest emitted after the DOM commit. A selector or transport beacon without matching DOM content is not a passing render proof.

## Screenshot procedure

Use a fixed viewport, font-loading policy, device scale, theme class, and random seed. Wait for fonts and the final paint before capture. Redact or reject unexpected text before persisting an image. Keep baseline images and their license/provenance with the public test assets.

## Failure triage

First compare the serialized fixture and DOM proof. If both are unchanged, inspect CSS and font loading. A changed screenshot containing a real hostname, path, transcript, or endpoint is a fixture failure and must not be accepted as a baseline.
