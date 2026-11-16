# Desktop render-evidence contract

An end-to-end walk must prove that the expected content was committed to the DOM. Transport events, store selectors, and generic counters are useful diagnostics but are not render evidence by themselves.

## Paint proof

The preferred beacon is `chat:slot_painted.contentDigest@1`:

```text
{
  rowCount: number,
  kinds: { [kind: string]: number },
  lastRowDigest: null | {
    kind: string,
    displayRule: string,
    text_prefix: string
  }
}
```

Only the newest row carries a bounded text prefix. A reply assertion waits for a new paint whose last row kind is `ASSIST_TEXT`; a pre-send sequence number prevents an old paint from satisfying the check.

## Question proof

Question walks derive expected option labels from the parsed fixture payload and compare them with the rendered option buttons. The comparison requires equal counts, exact labels, and no prompt or terminal noise. Count only question controls or transcript rows, not surrounding chrome.

## Negative proofs

Each important regression should include a negative control. For example, a storm of selector events without an assistant paint must fail, and a store count increase without an assistant paint must fail. The positive control then emits the paint and passes without modifying production code for the test.

## Evidence convention

Persist a compact verdict object with the fixture id, candidate id, beacon sequence bounds, DOM counts, and assertion results. Do not persist inline transcripts, private paths, screenshots from a real device, or external endpoint details.
