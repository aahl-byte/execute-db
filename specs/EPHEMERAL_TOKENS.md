---
title: Ephemeral Tokens
summary: Short-lived, password-free access to one environment, minted as an encrypted credential snapshot whose key material self-destructs at expiry.
intent: "An environment's credentials may be password-encrypted at rest, and the password can only be typed on a real terminal — which means an unattended caller (a script, a scheduled job, a coding agent) can never use that environment. Ephemeral tokens are the one delegated path: the human pays the password cost once, at creation, and hands back a bearer secret that works without a terminal until it expires. The design's whole burden is making 'until it expires' mean something on the client side, where a ciphertext cannot refuse to be decrypted after a deadline."
parent: ARCHITECTURE.md
children: []
sources:
  - db_core/core/tokens.py
  - db_core/core/keyring.py
  - db_core/commands/token.py
tags: [tokens, credentials, expiry, keyring]
---

# Ephemeral Tokens

`db_core/core/tokens.py` is the whole model; `db_core/commands/token.py` is
presentation only. That split is load-bearing and worth keeping: the core
functions return data (a `TokenResult`, a list of wiped ids, a count) and the
command layer decides what the terminal sees. Anything in the core that prints
is a bug in the making, because the same functions are called from a systemd
timer with nobody watching.

## What a token IS

A token is **a copy of an environment's credentials, re-encrypted under a fresh
random secret, with the expiry sealed into the authenticated header**. It is not
a reference, a pointer, or a capability that gets checked against the store. It
is a snapshot.

`create_token` reads the source env through `store.read_env_text` — which is
where the password prompt happens, if the env is encrypted — and writes the
decrypted text straight back out under a new passphrase into
`<config dir>/.ephemeral/.env.<tid>`. The token secret itself is
`secrets.token_urlsafe(16)`; the `tid` is the first 12 hex of its SHA-256
(`token_id`), so the file can be found from the token without the file ever
storing the token. The token is printed once and is not recoverable — nothing on
disk contains it.

Nearly every consequence below falls out of "snapshot, not reference":

- Changing or deleting the environment does not change or delete its tokens.
  They already have their own copy.
- The token file carries **no environment identity**. The plaintext inside is
  just `DATABASE_URL=...`, and the header holds only a magic and an expiry.
- Two tokens for the same env are unrelated files with unrelated keys.

See `CREDENTIAL_STORE.md` for the on-disk format, the magic/expiry header, and
encryption at rest — this spec assumes it and does not restate it.

## The kernel keyring share is the clever part

The hard problem: a ciphertext can't enforce a deadline. Whoever holds the
ciphertext and the key can run the math forever, and "delete the file at expiry"
is defeated by the trivial attack of copying the file. Any design that leans on
a scheduled deletion is asking a *file* to be the secret.

So the secret is moved into the key material instead. `create_token` mints a
random 32-byte **key share** and stores it *only* in the kernel keyring
(`db_core/core/keyring.py`) with a kernel-enforced TTL. The file is encrypted
with `token_passphrase(token, share)` — literally `"<token>:<share>"` — and the
share never touches disk. At expiry the kernel destroys the share with no user
process involved, and from that moment **every copy of the token file, wherever
it was taken, is permanently undecryptable — even by someone holding the
token**. A reboot destroys it too, which is why tokens deliberately do not
survive one. `revoke_token` destroys the share as well, which is what makes an
early revoke bite copies and not just the original.

That is the difference worth protecting when modifying this code: the wipe
timers below delete a file, which is hygiene; the share expiring is the actual
security property. If you ever find yourself "fixing" a case by removing the
share, you have traded the property for the hygiene.

`keyring.py` has two subtleties that are easy to break:

- A key is added to the **thread** keyring first, because a fresh key grants
  full rights only to its possessor — the timeout and permissions must be set
  while we still possess it — and only then linked into the anchor. If the
  link or timeout fails the key is revoked rather than left behind: never leave
  an immortal share in the kernel.
- The anchor is `@u` normally, but the **persistent** keyring in system mode
  (`_anchor`). `@u` is only guaranteed to live while some process of the uid is
  running, and separate `sudo` invocations of the service user leave gaps in
  which it can be reaped, taking live shares with it. The persistent keyring
  survives those gaps. It falls back to `@u` when persistent keyrings are
  unavailable. See `PRIVILEGE_SEPARATION.md` for what system mode is.

The keyring is Linux-only and callers must treat an unavailable keyring as "no
share" — never as an error.

## `bound` and `scheduled` are honest degradation

`TokenResult` reports two booleans that are not about success or failure of the
mint, but about *what kind of token you actually got*:

- **`bound`** — did the key share make it into the keyring? If not, the
  passphrase degrades to the token alone (`token_passphrase` returns bare
  `token` when the share is falsy). The token still works. It just has no
  self-destruct, so a copied file stays decryptable with the token forever,
  including after expiry.
- **`scheduled`** — was a transient systemd timer set to wipe the file at
  expiry? If not, the file lingers until something sweeps it.

Both degradations are deliberate: refusing to mint a token because systemd is
absent would be a worse tool. But degrading **silently** would be a lie — the
user would believe they had a self-destructing credential when they had a
plain bearer secret in a file. So `cmd_create` prints the good cases to stdout
and routes both bad cases to **stderr** as warnings. The rule to preserve: the
core reports the fact, the command layer is responsible for the user knowing
which of the two tools they are holding. A token that quietly lost its share is
the single most dangerous thing this module can produce.

## Expiry is enforced in depth

Five mechanisms, and they are not equals. Know which are load-bearing:

| Layer | Role |
| --- | --- |
| Expiry sealed in the authenticated header | **Load-bearing.** Authenticated as AAD, so editing it makes decryption fail. Expiry is tamper-*evident*, not merely recorded. |
| Kernel keyring TTL on the share | **Load-bearing.** The only thing that makes expiry survive file copies. Everything else assumes the attacker didn't copy. |
| Transient systemd user timer (`schedule_token_wipe`) | Backstop. Wall-clock deletion of the file at expiry, so it isn't sitting around after it stops working. |
| Boot sweep timer (`install_boot_sweep`) | Backstop for the backstop. Transient timers do not survive a reboot; a persistent user timer sweeps ~2 min after startup to catch what they dropped. |
| Best-effort sweep on every CLI run (`db_core/cli.py`) | Backstop. Wrapped in a bare `except` and skipped for `token` commands (which sweep verbosely for themselves). It must never break the actual command. |

The two crypto layers make the token *stop working*. The three sweeps make the
dead file *go away*. Conflating them is the mistake to avoid: if the sweeps all
fail, an expired token is still refused and still undecryptable once the share
is gone. That is the whole point of putting the deadline in the key material.

`schedule_token_wipe` pins `HOME` into the transient unit so the sweep targets
the same config dir that minted the token, and asks for the timer at
`ttl + 2` seconds — the same slack as the keyring TTL, so the sweep doesn't race
the deadline from the wrong side. `install_boot_sweep` writes its units once,
returns early if they already exist, and fails silently by design: it is not the
thing that reports to the user, `schedule_token_wipe` is.

`sweep_expired` reads the expiry with `crypto.expiry_of` — an *unauthenticated*
header read — and wipes anything past due. That is fine precisely because the
worst an attacker achieves by forging a past expiry in a header is deleting a
file they already broke: editing the header destroys decryptability anyway.
Reading is cheap; the sweep never needs the share and never decrypts.

## Decrypt first, then check expiry

`load_database_url_from_token` does, in order: hash the token to a tid, confirm
the file exists, read the share, **decrypt**, then check expiry, then extract
the URL.

The ordering is not stylistic. `crypto.expiry_of` only *parses* the header; a
successful decrypt is what *authenticates* it, including the expiry, because the
header is the AEAD's AAD. Checking a parsed expiry before decrypting would be
checking a number an attacker could edit. Nothing may reorder these.

The error text is also deliberate. A failed decrypt with no share in hand says
so explicitly — "its kernel key share has self-destructed (shares expire with
the token and do not survive a reboot)" — because the alternative, a bare
"Invalid token", sends the user hunting for a typo when the real answer is that
the machine rebooted. With a share present, a decrypt failure genuinely is a bad
token and says only that. (`ERROR_DISCLOSURE.md` owns the broader rules about
what may be told to whom.)

## Tokens carry no environment identity — so `config rm` revokes everything

`config rm <env>` calls `revoke_all_tokens`, not "revoke this env's tokens".
There is no such function and there cannot be one: a token file is a
self-contained encrypted URL snapshot, and `rm` cannot decrypt it — it has
neither the token nor the share — so it cannot ask a file which environment it
came from. `TokenResult.env` exists only to print at creation; it is never
persisted.

The honest choices were: revoke all, or revoke none and leave live credentials
for a deleted environment. Revoking all is right, and `cmd_rm` says so, then
points at the only real remedy (rotate server-side). Keep `revoke_all_tokens`
best-effort per token — one failure must not strand the rest — and note that it
kills each share regardless of whether the file wipe succeeds.

`CREDENTIAL_STORE.md` cross-references this reasoning; the schema cache
(`SCHEMA_INTROSPECTION.md`) is cleared wholesale for the same shaped reason,
keyed by URL hash rather than env name.

## `token_path` validates the tid before building a path

```
token revoke ../../.env.production
```

That is the comment's own example, and it is why `token_path` matches the tid
against `TOKEN_ID_RE` (`^[0-9a-f]{12}$`) before joining it to the ephemeral dir.
Without the guard, a user-supplied id would let `revoke` — which wipes whatever
path it is handed — escape the directory and destroy an arbitrary file.

The general rule, and the reason it's worth stating: **the guard belongs at the
boundary where a value first becomes a path component, not at every call site**.
`token_path` is the only place a tid becomes a path, so it is the only place
that needs to check. Values derived internally (`token_id` output, filenames
enumerated from a `glob`) are already constrained by construction and need no
such check — which is why `sweep_expired` and `revoke_all_tokens` operate on
globbed `Path` objects directly rather than round-tripping ids through
`token_path`. Any new user-supplied path component should copy this model:
validate once, at the construction site, and fail closed.

## `parse_ttl` vs `parse_duration`

`parse_duration` is the shared `45s/30m/2h/1d` parser. It allows zero and takes
a `flag` argument purely so the error message can name the option that was
wrong.

`parse_ttl` wraps it and adds the rules that are specific to a **credential
lifetime**: zero is nonsense (a credential that expires the instant it exists),
and in hardened mode the TTL is capped at `system.MAX_SYSTEM_TTL_SECONDS` (24h).

The split exists so that a *cache* bound cannot silently acquire a
*credential's* constraints. `schema --max-age` (see `SCHEMA_INTROSPECTION.md`)
calls `parse_duration` directly and deliberately inherits neither rule: zero
legitimately means "bypass the cache", and a 48h staleness bound has nothing to
do with how long a credential may live. If you add a rule, be deliberate about
which function it lands in — putting a credential rule in `parse_duration`
silently changes what `--max-age` accepts.

## System mode differences

Under the hardened install (`PRIVILEGE_SEPARATION.md`), three things change:

- `--ttl` is capped at 24h.
- Shares anchor in the service user's **persistent** keyring, because `@u` can
  be reaped between `sudo` invocations.
- `schedule_token_wipe` returns `False` immediately: there is no user session or
  bus, `systemd-run --user` would just fail noisily, and `install.sh` installs a
  **system** timer that sweeps every minute instead. `install_boot_sweep` bows
  out for the same reason.

The installer also deliberately does not migrate `.ephemeral/` into the service
user's store: tokens are uid-bound, and a share in your keyring is meaningless
to the service user. Tokens do not survive the transition to hardening.

## A read-only token is not an execute-db token

`explore-db` keeps its own store, so its ephemeral dir is a different directory
and `share_desc` is namespaced by the app name (`<app>:token:<tid>`). An
`explore-db` token is therefore invisible to `execute-db` — not rejected by a
check that could be bypassed, but absent. Read-only access you delegate cannot
be replayed as writes. Preserve this by never resolving token paths or share
descriptions against anything but the *active* app.

## The honest limit

Client-side crypto cannot revoke knowledge. Everything here — the sealed expiry,
the self-destructing share, the timers — constrains what a holder can do with
*this file* on *this machine*. None of it touches the credential itself. An
agent holding a live token can read the URL out of its own process and connect
directly forever; `revoke` will not follow it.

So the spec must not imply that revocation is real. It is real for the file, and
that is a genuinely useful property against copies and against tokens outliving
their purpose. To actually cut off exposed credentials you act server-side:
rotate the database password, or issue roles with `VALID UNTIL` so the server
refuses logins after a deadline. `cmd_rm` and the README both say this at the
moments a user is most likely to believe otherwise, and that placement is worth
keeping.

## Gotchas for anyone modifying this

- `list_active` does **not** filter expired entries — it lists what is on disk.
  `cmd_list` sweeps first, which is what makes the listing look filtered. A new
  caller that skips the sweep will show tombstones.
- `token_passphrase` tests the share for falsiness, so an empty-bytes share is
  treated as no share. Deliberate, and it means `bound` is the only honest
  signal about which key form was used.
- `keyring.read` uses a fixed 512-byte buffer. The share is 64 hex chars today;
  a much larger payload would need the buffer grown.
- `revoke_token` removes the share **before** checking whether the file exists,
  and returns `False` only to mean "no file was there". The share dies either
  way, which is right: a missing file is not evidence the share is gone.
- `create_token` calls `install_boot_sweep()` on every mint. It is cheap and
  early-returns, but it is a filesystem+`systemctl` side effect on a hot-ish
  path; don't add more work to it without noticing.
