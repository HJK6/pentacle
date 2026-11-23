# Forge model library

Forge accepts a local directory containing `model.json` and one
self-contained GLB per view. This delivery adds the publisher, reusable
viewer and read API, not dashboard, chat, phone, migration or deployment.

## Sheet and measurements

The executable [full schema](../lib/model_drop.schema.json) disallows unknown
properties at every level. IDs are lower-case letters, digits and hyphens,
start with a letter or digit, and are 1–80 characters. Views and parts are
unique within their respective parents. Dates must be real and not later than
today UTC. Descriptive fields are nonblank, single-line plain text; material
may be empty for non-print roles. Facts accept only absolute HTTP(S) URLs.
Each part's description is distinct within its view (case and whitespace
normalized), one or two sentences. A `.glb` file name can have capitals and
underscores; it is not a view ID. Paths use forward slashes, cannot contain
parent segments or symlinks, and may not reference outside resources.

Complete synthetic example (`Sample_A.glb` has one mesh node named `Spacer A`):

```json
{
  "schema": "model-drop.v1",
  "project": {"id": "bench", "title": "Bench fixtures"},
  "id": "spacer",
  "title": "Test spacer",
  "updated": "2025-01-12",
  "summary": "A spacer keeps two test blocks apart.",
  "facts": [{"label": "Use", "value": "Fixture spacing"}],
  "changes": [{"label": "Width", "from": "8 mm", "to": "10 mm"}],
  "unverified": ["The fit has not been tested."],
  "units": "mm",
  "up": "z",
  "views": [{
    "id": "front", "title": "Front", "file": "Sample_A.glb",
    "camera": {"turn": -35, "tilt": 25},
    "parts": [{
      "id": "spacer", "name": "Spacer", "role": "print", "material": "PLA",
      "color": "#708090", "about": "The block sets the gap between the fixtures.",
      "nodes": ["Spacer A"], "explode": [0, 0, 12]
    }]
  }]
}
```

One GLB unit is one millimetre; there is no automatic metre conversion.
Export static, named triangle-list meshes from the default scene with dense
float positions and embedded bytes. All its mesh nodes must be claimed exactly
once. This version does not silently approximate sparse or compressed
geometry, skinning, morphs, lines, points, strips or fans; export as static
triangles. The viewer uses sheet colours rather than GLB materials or textures.
Part dimensions are actual triangle-referenced local position bounds across
all primitives, sorted largest first, reported to 0.1 mm. Copy comparisons
use the unrounded sorted values with 0.1 mm tolerance. Counts are node copies
and placed triangle instances, respectively. Overall extents are measured in
the model's X/Y/Z axes after all node and parent placement. Keep rotations as
node transforms to avoid changed dimensions from off-axis baking.

Byte and triangle ceilings apply independently per view. Defaults are
33,554,432 bytes (32 MiB) and 500,000 triangles, inclusive. Override with
`--max-bytes <positive-integer>` and `--max-triangles <positive-integer>` on
`check` or `publish`. The command reports both counts when the GLB is
measurable. These are provisional fixture defaults, not production calibration.
No mesh simplification is performed.

## Local commands and recovery

```sh
npm ci
npm run build:web
node scripts/model-library.cjs check --drop ./drop
node scripts/model-library.cjs publish --root ./library --drop ./drop
node scripts/model-library.cjs verify --root ./library
node scripts/model-library.cjs link-preview --root ./library --project bench --model spacer --view front
node scripts/model-library.cjs remove --root ./library --project bench --model spacer
node scripts/model-library.cjs rollback --root ./library --project bench --model spacer
```

`--root` takes precedence over `PENTACLE_MODEL_LIBRARY_ROOT`. `check`
does not write; its refusal lists field locations and fixes. `publish`
checks the current project title; use `--rename-project` explicitly to
change it. Run only one writer per root; do not modify the library while it
publishes. A model's `current.json` contains one selected revision and the
retained selections in reverse selection order. Objects are content-addressed
and shared across revisions. The revision hash covers the canonical sheet,
view inventory, measurements, asset digests and publication time. An
identical verified output reuses the selected revision and its time without
rewriting the pointer. A sheet-only revision reuses the existing GLB object.
The immutable manifest and objects are verified before one atomic pointer
rename. Refusal before selection preserves the old pointer; a hard process
termination before selection may leave unselected files, never a newly
selected partial revision. Do not edit manifests or object files.

`remove` deselects without deleting files. `rollback` defaults to the first
retained selection and accepts `--revision <retained-revision>`.
`link-preview` is local to a trusted reader. It picks the first view when
`--view` is omitted. Verification checks all installed revisions and object
hashes. Readers verify each model and retain the last verified selection if a
replacement copy is incomplete. A cold reader omits that model until the
selection verifies. Restore the missing bytes from a verified backup, then
run `verify`; never recompute a manifest over damaged data. Removed models
are absent from the catalog; verified retained objects remain readable at
their immutable addresses by the same operator.

## Local viewer and pictures

The web build ships Three.js **0.180.0**, **MIT**, in
`/next/model-viewer.bundle.js`; normal pages do not load it. The reusable
module provides pointer orbit, shift-drag slide, wheel zoom, programmatic
fit/pull-apart, selected-part edges and projected part rectangles. At camera
turn and tilt zero the model is viewed from −Y (Z-up) or +Z (Y-up), with +X
screen-right. Positive turn moves toward +X, positive tilt raises the camera.
Defaults are −35° and 25°.

Publishing uses the Design recorder's sandboxed Chrome policy and default
WebGL 2 backend. Build first and point `PENTACLE_CHROME` (or `CHROME_BIN`)
at the installed browser if necessary. The offline page uses only local
assets and writes one 448 × 280 transparent PNG per view. Exterior points
within 2 px of the undecorated coverage mask (alpha at least 32/255) get the
fixed RGB (130, 130, 130) stroke. This is separate from interactive selection.
Measurement decodes the PNG, uses exterior position and a ±3 per-channel
tolerance, and requires at least 90% of stroke pixels to reach 1.5:1 contrast
after compositing over each dark, light and cosmic theme well. Fill pixels
cannot satisfy the exterior coverage requirement. Failed Chrome/WebGL,
empty geometry projection or invalid coverage refuses before any write.

## Operator API

See the [web host trust boundary](../server/README.md#forge-read-binding).
An example operator binding (all values synthetic):

```sh
PENTACLE_MODEL_LIBRARY_ROOT=./library node server --auth tailscale \
  --allow-login forge-operator@example.test --operator-login forge-operator@example.test \
  --origin https://fixture.example.test
```

The server resolves one verified proxy identity, including during `/cc`
upgrade. A missing binding, an ordinary token cookie, a client role flag or
the anonymous loopback exception do not confer the entitlement. Unadmitted
callers get 401. Admitted non-operators and unknown targets get 404 without
private validators or metadata. Design admission is unchanged.

`GET /api/models` (also HEAD) returns `model-catalog.v1`, ordered by sheet
`updated` descending; ties use ascending IDs. Each model exposes the checked
sheet, selected revision, `published_at` and view measurements and URLs.
`published_at` describes the selected immutable revision, including on
rollback. Media: `/model-media/<project>/<model>/<revision>/mesh/<view>`,
`/picture/<view>` and `/picture` (first view). Authorized matching ETags get
a bodyless 304, and media support single byte ranges. All these responses
use `Cache-Control: private, no-store`. There is no network preview or
attachment fallback. Future projections must use the same server predicate
for the viewing recipient and omit Forge when recipients cannot be distinguished.

## Candidate checks

```sh
npm test
npm run build:web
node test/e2e/web_gate.js --scenario model-library-publish --scenario model-library-picture
node test/e2e/web_gate.js
```

The standalone minified viewer bundle is 504,078 bytes uncompressed. The
repository gate used Node 22.23.5. The candidate's focused tests (including
the existing web-auth, theme and Chrome-policy tests) passed. Repository
`npm test` encountered the two inherited private-tmux socket checks in this
sandbox; see the accompanying evidence for the test-only base comparison
and counts. The web build passed. The focused browser gate reached the first
publish in both scenarios but could not start sandboxed Chromium, and the
full web gate stopped before CDP. Neither browser scenario, the full web
journey, nor native Windows E2 was verified in this environment.
