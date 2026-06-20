# 🍀 Clover v2 — Sending Spec: Reply / Reply-all / Forward (direct send, shipped disabled)

> Status: **BUILT — shipped DISABLED by default.** The "delivery" track from the
> roadmap. This is the first capability that lets Clover **write to your mailbox and send mail**, so
> it is gated off until explicitly enabled. Created 2026-06-19.
> Behaviour doc: [`HOW_CLOVER_WORKS.md`](HOW_CLOVER_WORKS.md) (updated when built).

## Purpose

Let the operator reply, reply-all, and forward archived emails and **send them directly** via SMTP,
from inside the Mail viewer — turning the read-only archive into a working mail surface — **without
ever auto-sending** and without sending being possible until deliberately switched on.

## Principles

- **Off by default, fail-closed.** `sending.enabled` defaults to `false`. Two independent gates: the
  UI hides compose actions, and the send route hard-refuses when disabled. A crafted request cannot
  send while off.
- **Never auto-send.** Every send is a deliberate user click behind an explicit confirmation that
  shows the exact recipients. Clover never sends on its own, on a timer, or as a side effect.
- **Irreversible action = explicit consent.** Sending reaches real third parties and can't be
  recalled; the confirm step names every recipient and the subject.
- **Pluggable, like ingestion.** A `MailSender` ABC + registry (SMTP first), mirroring `MailSource`.
- **Auditable.** Every send is logged (UTC time, recipients, subject, message-id) to the runtime log.

## Scope

In: Reply / Reply-all / Forward compose in the Mail thread view; SMTP send; save a copy to Sent
(IMAP APPEND); SMTP config + enable toggle in Setup; confirm-every-send; audit log.
Out (later): HTML compose, contacts/autocomplete, scheduling/queue, multiple identities, OAuth-SMTP,
draft saving (we send directly per operator decision).

## Configuration (`clover_config.json` → new `sending` block)

```json
"sending": {
  "enabled": false,
  "smtp": { "host": "", "port": 587, "security": "starttls" },
  "from": "",                 // default = auth.imap.user
  "save_to_sent": true,
  "sent_folder": "Sent"
}
```
- SMTP password stored in the **OS keyring** (key `clover_v2_smtp_password`), never in config/logs.
- `enabled` is the master switch (Setup toggle, default off).

## Architecture

1. **`MailSender` (ABC + registry)** — `clover/senders/`; `SmtpSender` first. `send(msg) -> message_id`.
2. **Compose builder** — `clover/compose.py` (pure): given the original `.eml` + action + user text,
   build a correct RFC822 reply/reply-all/forward `EmailMessage` (recipients, `Re:`/`Fwd:` subject,
   `In-Reply-To`/`References` threading headers, quoted original, forwarded attachments). Unit-tested
   with no network.
3. **Source write** — add `append(folder, raw_bytes)` to `MailSource` (IMAP APPEND) for the Sent copy;
   default `NotImplementedError` on sources that don't support it.
4. **Routes** (all behind the enabled-gate) — `POST /threads/{id}/compose` builds a preview;
   `POST /send` validates + sends + saves to Sent + logs. Guard returns 403 when disabled.
5. **UI** — Setup gains an SMTP + "Enable sending" section; the thread view gains Reply/Reply-all/
   Forward (only rendered when enabled) → compose panel → Review & send → confirm modal.

## Send flow

compose (in thread) → **Review & send** → **confirm modal naming every recipient + subject** →
`SmtpSender.send()` → on success, **APPEND a copy to Sent** → audit-log → success notice. Any failure
is surfaced and nothing is silently retried.

## Safety / gating (must hold)

- `sending.enabled == false` ⇒ compose UI hidden **and** send route returns 403. Default off.
- No SMTP password / host ⇒ cannot send (validated before any network call).
- Confirmation naming recipients is mandatory; no "remember/skip".
- A copy of everything sent lands in Sent; every send is audit-logged.
- Read paths are unchanged — archiving/threading stay read-only; only sending + the Sent-copy write.

## Acceptance criteria

- With `enabled=false` (default): no compose buttons render, and `POST /send` returns 403 — verified
  by tests. Nothing can leave the mailbox.
- With `enabled=true` + a stub sender: reply/reply-all/forward build correct recipients, subject,
  threading headers, and (forward) carry attachments — verified by tests using a fake sender (no real
  SMTP, no network).
- Confirm step lists exact recipients; send is never triggered without it.
- Sent copy is appended; send is audit-logged.
- SMTP password never written to config or logs.
