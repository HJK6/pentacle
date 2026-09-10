# Cosmic theme guide

The cosmic theme is a scoped presentation layer for the desktop chat surface. It provides palette, type, spacing, machine-token, status, and severity values without coupling the renderer to a particular host.

## Scoped tokens

The `.cosmic` root exposes `--cosmic-*` variables. Keep non-chat application styles in the ordinary root layer so the theme can be enabled or removed without changing layout code.

The desktop resolves cosmetic sigils separately from machine names. Stable sigil names are `djinni`, `sun`, `mage`, and `flower`. Host labels and colors come from desktop configuration; sigils do not define a machine inventory. See [desktop configuration](desktop_config.md).

## Components

The TypeScript component module exposes factories for sigils, ring frames, starfields, bevels, provider tags, status tags, severity tags, progress bars, and small icons. Factories return live DOM nodes or SVG elements; callers own insertion and removal.

All generated text is assigned with `textContent`. SVG attributes are set through a narrow helper. Star positions use a deterministic seed so screenshot tests remain stable.

## Typography and contrast

Display text uses the bundled display face when available and a system sans fallback. Metadata and code use a monospace fallback. Every status and severity color must also have a text or shape cue; color alone is not the accessibility contract.

## Integration

Load the theme once, add `.cosmic` to the chat root, and use the component entrypoint's browser global only when the app is not bundled. The token entrypoint is independent from the chat store. A public build may substitute system fonts or a different palette while retaining the same token names.

## Visual tests

The shared TypeScript scene builder in `test/cosmic_scene_builder.ts` uses a
synthetic `mage` stream and seven deterministic states. Structural tests and
the screenshot generator consume the same builder; see
[visual regression](desktop_cosmic_visual_regression.md). Sigil names describe
presentation, independently of the configured fleet. Do not use live dashboard
data, captured transcripts, or real machine labels as visual fixtures.
