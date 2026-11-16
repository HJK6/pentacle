# Shared web client request scheduling

Browser tabs share the web host's daemon connection. Ordinary RPCs use a
client queue with six active requests and 6 MiB of encoded frames, below the
daemon's unchanged eight-request / 8 MiB connection limits. Background reads
can occupy four active slots; interactive ordinary requests take precedence
over waiting reads. Durable send/answer, parked waits/transcription and
control/diagnostic verbs retain their independent daemon-pool routing. The
classification uses the wire verb, never the display prefix.

The waiting queue holds at most 64 requests / 16 MiB. Background waiting is
limited to 48 requests / 8 MiB, reserving capacity for user actions. An
oversized frame or new request exceeding these bounds fails readably; it does
not evict another caller or raise daemon limits.

Existing caller deadlines include queueing and retry delay. Without a caller
deadline, waiting is bounded to 30 seconds and the active RPC retains its
existing lifetime. A dispatched timeout rejects the caller but keeps a local
credit for up to 60 more seconds, until a terminal response or natural socket
disconnect. Expiry releases local accounting only. The client never drops the
shared socket to reclaim a credit. The daemon remains authoritative and may
refuse later work if the timed-out handler is still running.

Read-only background RPCs and image upload initialization retry once, after
250 ms, only on an explicit `server_overloaded` error with `retryable: true`.
They keep the same socket, request ID and original deadline. Mutations, sends,
timeouts, disconnects and unknown outcomes are never replayed. Disconnect and
destroy reject queued requests and cancel their timers.

Protocol-1 image chunks keep their existing wire format. A refusal latch is
registered before the first nonfinal chunk and remains while the final chunk
is queued. A refusal cancels a waiting final and fails the upload; chunk
errors never trigger a retry. Success also requires the returned blob digest
to match the entire original image. Nonfinal chunks have no acknowledgements
and retain their existing direct transmission path: large-image chunk burst
admission is a separate residual, not covered by the ordinary RPC window.
