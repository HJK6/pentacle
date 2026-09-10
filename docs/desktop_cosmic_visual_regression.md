# Cosmic visual regression

Visual regression tests for the desktop chat surface use only deterministic fixtures. They do not connect to a live daemon, install a package, or read another application.

## Fixture matrix

Render these cases:

1. an idle `coordinator` session with an empty transcript;
2. a working `workstation` session with one assistant row;
3. a question for `linux-workstation` with two options and one selected option; and
4. a `satellite` session with a warning status and a short update history.

Each fixture supplies a stable stream id such as `coordinator:visual-fixture`, a fixed timestamp, and bounded text. It should be serializable JSON stored beside the test, with no absolute paths or external URLs.

## Assertions

Assert layout roles, labels, visible row counts, option labels, status text, and the content digest emitted after the DOM commit. A selector or transport beacon without matching DOM content is not a passing render proof.

## Screenshot procedure

Use a fixed viewport, font-loading policy, device scale, theme class, and random seed. Wait for fonts and the final paint before capture. Redact or reject unexpected text before persisting an image. Keep baseline images and their license/provenance with the public test assets.

## Failure triage

First compare the serialized fixture and DOM proof. If both are unchanged, inspect CSS and font loading. A changed screenshot containing a real hostname, path, transcript, or endpoint is a fixture failure and must not be accepted as a baseline.
