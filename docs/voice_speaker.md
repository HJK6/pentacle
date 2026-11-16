# Local spoken conversations

The microphone service owns the speech capability. A room-microphone capture claimed through `POST /wake/claim` gains an opaque `conversation_id`; local spawn actions do not. Typed messages and phone dictation cannot open a conversation through the speech API. Capabilities are process-local and expire on restart.

`POST /speak` accepts JSON with `conversation_id`, `kind` (`reply` or `announcement:<name>`), `text`, optional `action`, and optional boolean `final` (default false). It returns `outcome: spoken`, `suppressed` with `reason`, or `refused` with `reason`. Every kind needs an open conversation. The shipped announcement list is empty. The shipped action list permits `chat`; the service validates the action label and does not execute desktop actions.

Accepted spoken or suppressed lines count toward the limit. A final accepted line closes the conversation; the fourth accepted line also closes it. Refused lines leave it open. A minimum-gap suppression counts as an accepted line. Closed, expired, unknown, exhausted, disallowed-origin, over-length, disallowed-action, disallowed-announcement and busy-listener requests produce no output. `POST /turn-ended` closes a conversation with no accepted line and attempts the fallback once. After an accepted line, it produces no output and preserves the conversation until a final line, the line limit or the ceiling closes it. Repeating it produces no output.

Acknowledgement uses six rotating pre-rendered phrases at capture end, before routing or claim in the accepted local-routing-off configuration. The capture keeps its conversation while waiting for a delivering client; claiming never repeats the acknowledgement. A reply that arrives during its own acknowledgement waits for that clip to stop. It may replace that acknowledgement's echo fence, but never another recognition generation or an active capture. Shipped `late_kickoff_enabled=false` suppresses the late filler indefinitely. Explicitly enabling it allows one attempt after the policy deadline. A busy listener refuses output; these clips are not queued for surprise replay. Each output fences recognition under the existing wake lock, and local-action output respects that shared fence.

## Provisioning

A mic-owned sibling process keeps Kokoro loaded while isolating its dependencies from ASR. It receives sentence text as JSON on stdin, writes WAVs to its configured directory and returns timings. No text reaches a shell. Playback of a sentence can overlap synthesis of the next sentence. ONNX uses two non-spinning CPU threads by default (`MIC_KOKORO_THREADS` overrides the count). The main mic process serves HTTP/status while synthesis runs.

Provision a speaker environment with `mic-server/requirements-speaker.txt`; configure absolute `MIC_KOKORO_PYTHON`, `MIC_KOKORO_MODEL`, and `MIC_KOKORO_VOICES`. Voice defaults to `bm_george` (`MIC_KOKORO_VOICE` overrides it). These are local settings, never repository paths. Set `MIC_SPEAKER_OUTPUT_DIR` and `MIC_VOICE_CLIP_DIR` outside shared memory. Owned synthesis WAVs are bounded to 256 files and 24 hours; clip files and manifests are retained. Clip provisioning removes its intermediate WAVs.

`MIC_SPEAKER_SINK=null` is the default. It reads real WAV frames and returns duration, paths and timing without opening an output device. Only the literal value `player` selects the provisioned `afplay` sink; unset or unsupported values use the null sink. Audio output requires the runtime owner's separate authorization. Source publication is not a playback or activation grant.

Create a local policy file from `voice_rules.DEFAULTS` and set `MIC_VOICE_RULES_FILE` to its absolute path. Host display labels belong in this file. No configured file uses the shipped defaults. Render clips without playback:

```sh
python mic-server/render_voice_clips.py --rules /configured/voice-rules.json --output /configured/voice-clips
```

The manifest records voice, phrase text, filename and SHA-256. Startup verifies each required clip against the policy. Changed phrases require reprovisioned clips before reload. `POST /speaker/rules/reload` reads the configured file; invalid schemas or missing/corrupt clips leave the last valid policy active. `/status` exposes `speaker.ready`, `model_loads`, `rss_bytes`, `sink`, service readiness/error, rules version/error/Modes, and `last_outcome`. `speaker.last_conversation` contains `delivery_pending` and epoch-second timestamps for capture end, acknowledgement start, routing, claim, the actual provider USER root, first accepted line and first available audio frame. Each newly recorded timestamp also emits `subsystem=voice_latency`, `bug_ref=voice-reply-latency-202609`, conversation id and host load. Suppressed output has no audio timestamp; no client leaves delivery pending. Recognition and output are not inferred from source/build readiness. Outcomes carry `subsystem=voice_speaker` and the bug reference.

## Policy

| Section | Shipped controls |
| --- | --- |
| `replies` | 2 sentences, 40 words, 300 characters, 4 lines, 3-second minimum gap, 12-hour ceiling, 15-second kickoff; late kickoff disabled |
| `clips` | Acknowledgements, late kickoff, fallback, silent/meeting confirmations |
| `origins` | `room_mic` only |
| `modes` | Silent suppresses clips and lines; meeting suppresses neither; switch phrases are exposed for the mode-control lane |
| `announcements` | Empty allow-list |
| `actions` | `chat` |
| `labels` | Local display labels for origins and modes |

This lane provides policy sections and silent state; mode-switch controls belong to the separate mode lane. Meeting recording does not imply silence. Resident local-action speech uses `MIC_VOICE_SPEAKER_MODE=resident` (the default). Existing explicitly configured local/SSH script consumers remain supported for deployments using that contract.

## Validation and rollout

Run `PYTHONPATH=mic-server python -m pytest mic-server/test` with the provisioned test interpreter. The suite forces a null sink and fails if the real player/wrapper is invoked. Hardware/ASR/model counterparts are injected; captured WAV decoding, policy, claim HTTP routing, conversation limits, rules reload and subprocess ownership are exercised directly.

`python mic-server/measure_speaker.py --output /external/latency.json` requires at least twenty runs and a null sink. It measures real resident synthesis and recorded clips with a fixture listener, reports p95 acknowledgement/first-frame timings, host load and model-load/RSS status. The wake-to-claim route is covered separately by the HTTP fixture journey. A latency miss under load is reported to the runtime owner; no target is silently changed.

Activation requires one owner-controlled runtime window, an exact previous running-file manifest/PID/plist preimage, a rollback command, and accepted public source. Preserve local vocabulary, device, action-host and ASR settings during the public-source reconciliation. Set the sink to null for activation and automated probes. Bind installed file hashes and live PID to the accepted artifact; verify `/status`, a refused unknown conversation and an isolated fixture journey. No operator test step or audible playback is required.

Reply limits are read from the loaded policy by both `/speak` and the helper. Length refusals include `limit_name`, `limit` and `measured`; refused lines consume no conversation line. Existing policy files must gain `sentences_per_line`, `words_per_line` and `late_kickoff_enabled` before activation. Invalid or old schemas leave the last valid policy loaded with an error; activation must check the expected version, values and error field. Local routing stays in source; deployments selecting all-to-assistant delivery set `MIC_LOCAL_ACTIONS=false`.

Keep `MIC_LOCAL_ACTIONS=false` on this build. With local actions enabled, a fast local response can take the recognition fence before the capture acknowledgement starts and suppress that acknowledgement. Startup logs a warning in this configuration. Do not re-enable local actions until the acknowledgement ordering fix ships; all-to-assistant delivery is the accepted configuration.
