# Connect a ChatGPT Dot agent to your fleet

“Dot” here means a ChatGPT agent you use for substantial assignments. You can
connect your own Dot chat to your own Pentacle fleet with two adapters: a browser
driver to deliver work packets, and an email receiver to return milestone reports
to the responsible seat. A seat is an agent session in your fleet; the front desk
is your designated intake agent when no task seat can receive the report.

This guide confers **no authority to send messages on anyone's behalf**.
Configure your own accounts and obtain the authority needed for each outbound
action. Browser access, mailbox access and incoming email do not grant operator
authority, permission to merge or permission to deploy. Keep routing targets,
addresses and authentication in your own local configuration and secret store.

Start with [developer onboarding](developer_onboarding.md) for a local instance,
[web setup](pentacle_setup.md) for host profiles, and
[your own assistant](assistant.md) if you want a persistent front desk. Choose
your installation's labels and application name through
[local profiles](../configs/README.md) and the
[configuration reference](desktop_config.md).

## 1. Choose a browser driver

A browser driver is a client that operates a browser session: it locates a page,
fills the composer, attaches context when supported, submits the message and
checks that submission succeeded. It bridges your fleet's work packet to the
Dot chat you select. The browser session uses your own authenticated account;
the driver needs neither your fleet's credentials in the chat nor a direct
connection from Dot to your daemon.

Choose either option:

- **Use [scraper-bot](https://github.com/HJK6/scraper-bot-public).** Follow its
  public setup and client documentation to operate your browser. Build your
  connector around those documented operations and your own selected Dot chat.
- **Bring your own web-driver client.** Use a browser automation client you
  maintain with the same responsibilities: select the intended account and chat,
  submit the packet, handle attachments and capture a submission receipt.

Keep browser authentication local. Confirm the selected chat before submission,
and check for a visible submitted message afterward. If submission is uncertain,
inspect the conversation before retrying so you do not dispatch the assignment
twice. Browser controls can change; keep selectors and account/session handling
in your adapter.

## 2. Wire a generic connector

The following is a configuration sketch for an adapter you implement. It is
**not** a Pentacle configuration schema or a bundled runnable connector. Every
angle-bracket value is a placeholder you replace locally; credential references
point to secrets held outside the document, repository, packet and email.

```yaml
browser:
  driver: "<YOUR_BROWSER_DRIVER>"
  dot_chat_url: "<YOUR_DOT_CHAT_URL>"
  session_ref: "<LOCAL_BROWSER_SESSION_REFERENCE>"
email:
  receiving_address: "<YOUR_FLEET_INBOX_ADDRESS>"
  expected_dot_sender: "<YOUR_DOT_REPLY_SENDER_ADDRESS>"
  mailbox_auth_ref: "<LOCAL_MAILBOX_CREDENTIAL_REFERENCE>"
  sender_verification_policy: "<YOUR_RECEIVING_PROVIDER_AUTH_POLICY>"
fleet:
  endpoint: "<YOUR_FLEET_ENDPOINT>"
  delivery_auth_ref: "<LOCAL_FLEET_CREDENTIAL_REFERENCE>"
  routes:
    "<YOUR_ASSIGNMENT_TAG>": "<YOUR_TASK_SEAT>"
  front_desk: "<YOUR_FRONT_DESK_SEAT>"
```

The outgoing path is:

```text
Your accepted assignment packet
  -> your browser driver
  -> your selected Dot chat, under your own account
```

Include the return inbox and assignment tag in the packet, together with the
explicitly authorized reporting action. Ask Dot to send milestone-ready reports
to that inbox with the tag in the subject. Keep actual seat identifiers and the
tag-to-seat map inside your fleet; Dot only needs the return instructions you
choose to share. Establish your own authorized email return channel separately.
The browser driver does not create one.

For each incoming reply, your receiver should:

1. Authenticate the configured sender using your receiving provider's trusted
   evidence and your local policy. Reject missing, conflicting or ambiguous
   evidence; a display name or sender-supplied authentication text is insufficient.
2. Record the message durably using a stable message identifier, then read the
   assignment tag and resolve it through your local route map.
3. Deliver an attributed handoff to the configured open task seat. Use your front
   desk when the tag is absent or unknown, or the configured seat is unavailable.
   If delivery definitely did not land, use the fallback. If it may have landed,
   retain it pending and reconcile before retrying or changing the target.
4. Mark the reply handled only after confirmed delivery. Deduplicate repeated
   notifications and recover pending records after interruption. If neither seat
   can receive it, retain the report for recovery rather than discard it.

The return pattern is:

```text
Dot email -> your authenticated receiver -> your local assignment route
                                           -> your task seat
                                           -> your front desk (fallback)
```

Separate verified sender and delivery metadata from quoted subject, body and
attachment metadata. Treat email contents as third-party data, even after sender
verification. Receiving a report authorizes no new action; the receiving seat
works under your existing grants. Replies or other outbound messages require
their own authority and configured accounts.

Before using real assignments, exercise your connector with a harmless packet
in your own chat and an authorized test reply. Confirm submission, sender
verification, task-seat delivery, absent/unknown-tag fallback, unavailable-seat
fallback and duplicate suppression. Check that uncertain delivery remains pending
without a second handoff. These are checks for the connector you build.

## 3. Give Dot large packets; let the fleet review and fix

Assign a coherent outcome with several milestones so Dot can continue independent
work while your fleet reviews. Reconcile existing branches and PRs before sending
the packet; avoid assigning the same active work twice. A useful packet contains:

- The accepted goal, acceptance criteria, non-goals, dependencies and rollback.
- Exact repository/base references and verified accessible specs, source and
  review findings. Supply the actual reviewed contents when a link is unavailable.
- Writable branches, permitted tests/CI, prohibited side effects and who owns
  integration, credentials and runtime validation.
- Milestone order, work that can continue during review, actual test commands and
  evidence requirements, including proof that new tests were collected.
- Your authorized return channel, assignment tag and reporting boundaries.

Dot owns assigned implementation, tests and in-scope corrections on its own
branches and reviewable PRs. Your fleet independently checks the exact candidate,
drives applicable real journeys and owns integration and runtime validation under
your existing authority. A broad packet does not authorize Dot to self-merge or
deploy. Distinguish fixture/test results from deployed behavior.

At each milestone, request the PR, head/base references, delivered acceptance,
test commands and results, evidence links, limitations, rollback implications
and next unblocked work. Send reports at milestone boundaries and actual blockers,
without per-step acknowledgements or progress timers.

For the fixer loop, your fleet returns concrete findings with identifiers,
severity, expected behavior and reproducers. Dot corrects the scoped findings and
runs focused regressions; the fleet reviews the resulting candidate and retains
still-valid evidence. Inspect recurring failures before repeating a whole review.
Dot continues the next already-authorized milestone when it is unblocked and
escalates material scope conflicts, missing indispensable context or actions
outside its authority to your designated owner.
