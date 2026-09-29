# Room microphone conversation

The mic service issues `conversation_id` when a room capture is claimed. The
delivering client carries it through binding handoff and prepends this machine
header to the assistant input:

```text
[pentacle-input {"origin":"room_mic","conversation_id":"opaque-service-id"}]

The recognized request.
```

Typed and mobile input use their existing send path and carry no such id. A
claim without a valid id stays held; upgrade the mic service and client together.
The header is context, not authorization: the local service refuses unknown,
expired and closed ids, including ids copied into a hand-written header.

## Spoken lines

Install the executable `bin/bart-say` on the assistant's PATH, for example with a
symlink to the accepted checkout. It takes literal text from stdin by default:

```sh
printf '%s' 'The task is finished. The details are in chat.' |
  bart-say --conversation-id opaque-service-id --kind reply --action chat --final
```

`--text` also accepts literal text; callers must quote it as shell data. The
helper sends JSON directly to localhost `POST /speak`; it never invokes a shell
with the text. `BART_SPEAK_URL` can select another loopback HTTP port for a test
service. Remote destinations, proxies and redirects are refused. The helper
prints JSON with `outcome` (`spoken`, `suppressed`, `refused`) and a reason for
non-spoken results. Refusal is nonfatal. The synchronous receipt allows the
speaker's thirty-second operation deadline plus five seconds for transport;
an unavailable endpoint does not change the chat reply and is never retried.

The checker enforces two sentences, forty words, three hundred characters, one
question, at most three numeric values and no links, identifiers, paths, code,
lists or markup. Numeric values must be rounded to at most one decimal place.
Semantic properties such as answer-first and a necessary question also require
independent transcript review. A short answer sets `--final`; long work submits
kickoff before delegation, a milestone only on a changed state, and completion
with `--final`. Kickoff and milestone omit the flag. Lines are written for the
ear; the full answer remains in the ordinary turn-final chat reply.

## Turn completion and tests

The delivering client registers only its own claimed captures. It observes the
matching provider USER root, then a structured final answer or the existing
provider turn-end marker, and posts `conversation_id` to `/turn-ended` once.
Send receipts, sidechains, working heartbeats and UI quiet timers do not end a
spoken turn. Strong request and optimistic IDs take precedence over text matching;
text is a fallback only when the provider root carries neither ID. The tracker
keys each delivery root separately, so answer turns can reuse a conversation ID.

Only the active provider root owns an uncorrelated final. A typed root occupies
that order without speech. If additional roots arrive while a provider turn is
active, they can be merged into that turn; their uncertain fallback is left to
the service's conversation ceiling. A missing fallback is preferable to closing
an ID before its later answer. Ordered reconnect backfill uses the same sequence
deduplication. The service owns fallback policy; an endpoint error never alters
chat state.

Focused checks:

```sh
node --test test/wake_delivery.test.js test/room_mic_delivery.test.js test/room_mic_turns.test.js
python3 -m unittest discover -s test -p test_voice_reply.py
```

Every voice test uses a fake endpoint or the speaker service's null sink. Tests
must never open an audio device. Evaluation fixtures use a disposable assistant
seat with a test mirror sink, never the operator chat or live assistant binding.
