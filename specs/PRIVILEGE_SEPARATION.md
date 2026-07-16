---
title: Privilege Separation
summary: The hardened install moves each tool's credential store under its own service user and re-execs the CLI there over a locked-down sudo rule, so a process running as you can use a credential but never take one.
intent: >
  Client-side crypto cannot beat a same-user adversary: a script or coding agent
  running under your account can read the secret files, read the kernel keyring
  share, and patch the CLI you type your password into. Privilege separation is
  the only answer that survives that adversary — put the secrets and the code
  behind a uid your account does not own. This spec records where that boundary
  actually falls, which parts of the machinery are load-bearing and which are
  only convenience, and the reasoning a future change must not quietly break.
parent: ARCHITECTURE.md
children: []
sources:
  - install.sh
  - db_core/core/system.py
  - db_core/commands/exec.py
tags: [security, hardening, sudo, privilege-separation]
---

# Privilege Separation

Everything in the plain install runs as you. That is a real limit, not a
shortcoming to be engineered around: anything else running under your account
can read `~/.execute-db`, read the kernel keyring share that a live token
depends on, ptrace the process holding a decrypted URL, or edit the CLI source
before you type your password into it. No amount of client-side encryption
fixes that, because the adversary has everything you have.

The hardened install (`install.sh`) draws a uid boundary instead. Each tool's
credential store moves to a dedicated system user's home; a root-owned frozen
copy of the package lives in a root-owned venv; and you reach the tool through
a root-owned launcher that re-execs it as its service user via a narrow sudoers
rule. Your account keeps the *ability to run queries* and loses the *ability to
possess the credential*. Hold that distinction — it is the whole design, and
most of the reasoning below falls out of it.

## The one thing to understand first: the boundary is the human

`system.maybe_redirect_to_launcher()` makes `execute-db` on your `PATH`
transparently hand off to `/usr/local/bin/execute-db` when it sees a `SYSTEM`
marker file in `~/.execute-db/`. Its docstring says plainly that this is **UX
and not a security boundary**, and that is the honest and correct reading:

- The marker lives in a directory *you* own and write. Deleting it disables the
  redirect. So does setting `EXECUTE_DB_NO_SYSTEM` (see `app.AppSpec`).
- `PATH` is yours to shadow. A hostile `~/.local/bin/execute-db` is reached
  before anything else and never has to redirect at all.

Neither of those is a hole to be plugged; they are structural. A program running
with your privileges cannot stop you — or something impersonating you — from
running a *different* program. The redirect exists so that a user who has
hardened their install doesn't get a confusing "no environments configured" from
muscle memory, and for no other reason.

What the redirect being subverted actually costs is small, and worth stating so
nobody over-defends it: a shadowed `execute-db` runs as you, reads
`~/.execute-db/`, and finds only the marker — no envs, no tokens. It cannot
reach the store. The real damage from a shadowed binary is that it can print
`Password for 'dev':` and capture what you type. **That** is why the README tells
you to invoke the trusted absolute path (or a root-owned alias), and why the
tool does not pretend to guarantee it for you. The security property is "the
human typed a path that root controls." Everything else is plumbing.

Corollary for anyone modifying this: do not add checks that try to make the
redirect trustworthy (signing the marker, verifying `PATH`, refusing to run
unless redirected). They cost complexity and buy nothing against an adversary
who simply never calls your code.

## What the install actually creates

Everything is derived from the app name, so `execute-db` and `explore-db` harden
independently and symmetrically — separate service users (`executedb`,
`exploredb`), homes (`/var/lib/<name>`), stores (`/var/lib/<name>/.<name>`,
mode `0700`), launchers, sudoers files, and sweep units. The Python side derives
the same names from `AppSpec` (`db_core/app.py`), which is why the shell and the
Python agree without a config file between them. Adding a third front-end means
adding it to `APPS` in `install.sh`; nothing else in this spec changes.

The two tools share exactly one thing: the root-owned venv at
`/usr/local/lib/db-cli/venv`, because one distribution provides both console
scripts. It is `chown -R root:root`, `chmod -R go-w`, and then *verified* with a
`find -perm /022` sanity check that aborts the install — this is the code-tamper
defense, and a silent failure of it would be worse than no hardening at all,
since the user would believe they were protected.

Hardening `explore-db` is not optional polish. Its store holds the *same*
`DATABASE_URL`. Leaving it readable would let an agent running as you lift the
connection string and connect read/write directly with `psql`, defeating
`execute-db`'s hardening entirely. Separate stores also scope tokens to a tool:
an `explore-db` token is not accepted by `execute-db`, so read-only access you
delegate cannot be replayed as writes.

## The sudoers rule, and why it is wider than it looks

```
Defaults!<cli> env_reset, always_set_home
<user> ALL=(<svc>) NOPASSWD: <cli> *
```

Read the trailing `*` carefully. It matches **any arguments**. Every flag the CLI
accepts is directly reachable as the service user by typing the `sudo` command
yourself — the launcher is a convenience wrapper over a rule that does not
require it. This is not a mistake (enumerating allowed argument patterns in
sudoers is a well-known way to write a rule that is both unusable and still
bypassable), but it means **the launcher can never be the enforcement point for
anything.** If a behaviour must not happen in the service-user process, it has
to be refused *in Python*, by the process itself. See the `-f` guard below; it
is the worked example.

`NOPASSWD` is deliberate. Prompting for your login password per query would
buy nothing: the same-user adversary can wait out a sudo timestamp or simply
ask you to run it. The gate on *use* is env encryption (a password read from
`/dev/tty`), not sudo. See `CREDENTIAL_STORE.md`.

`env_reset` is sudo's default, but stating it in *this* file makes the rule
independent of whatever `/etc/sudoers` says today. It strips the caller's
environment down to a minimal set, which is what keeps `PYTHONPATH`,
`PYTHONSTARTUP`, and `LD_PRELOAD`/`LD_LIBRARY_PATH` from crossing the boundary —
i.e. it stops you from injecting code into a process running as the service
user, which would otherwise hand back the decrypted URL and make the entire
uid split ornamental.

`always_set_home` covers a narrower case: under `env_reset` sudo already sets
`HOME` from the target user, so this flag only bites when a distro's
`/etc/sudoers` has put `HOME` in `env_keep`. It is cheap insurance against
inheriting someone else's `HOME` and pointing the config dir somewhere the
caller controls.

That insurance is doubled in Python, and intentionally: `store._home()` reads
the running uid's **passwd entry** rather than `$HOME` whenever
`system.in_system_mode()`. The store's location must not be a function of an
environment variable in a process the caller launched. Note that the shipped
sudoers already prevents the specific `sudo` without `-H` scenario its docstring
describes — the passwd read is the layer that still holds if the sudoers file is
edited, if the tool is reached by some other route (`su`, a systemd unit), or if
a future flag makes `HOME` reachable again.

## `-f/--file`: the model for any flag that names a path

This is the part most likely to be broken by a well-meaning change, so it gets
its own section.

The launcher (`install_launcher` in `install.sh`) parses `-f`/`--file` out of
argv **itself**, opens the file **as you**, and pipes it in on stdin:

```
exec sudo -H -u <svc> -- "$VENV_CLI" "$@" < "$FILE"
```

The remaining arguments are re-quoted and passed through. The service-user
process therefore receives SQL on stdin and never opens a path the caller named.

`exec.py`'s `run()` then **refuses** `-f` outright when `in_system_mode()` is
true. That looks redundant — the launcher already removed it — and that is
exactly the point: reaching that code with `-f` set means the launcher was
bypassed, which the sudoers wildcard makes trivial. The refusal is the actual
control; the launcher is the ergonomics.

The escalation it prevents is concrete. `-f` in a service-user process is an
arbitrary-file-read primitive scoped to that uid:

```
sudo -u exploredb /usr/local/lib/db-cli/venv/bin/explore-db \
     --dev -f /var/lib/explore-db/.explore-db/.env.dev
```

The service user can read its own store; you cannot. The file's contents are
then handed to Postgres as SQL, the server rejects them, and the syntax error
quotes the offending text back — with the `DATABASE_URL` in it. The credential
walks out through an error message. (Server-side errors *are* disclosed over
sudo, for good reasons that are not this spec's to relitigate; see
`ERROR_DISCLOSURE.md`, which owns that boundary. The two specs are load-bearing
for each other: this refusal is part of why disclosing server errors is safe.)

**Generalize this before adding any flag that names a filesystem path** — an
output file, an include, a cert, a `--config`. In system mode the process's
authority is not the caller's, so every caller-supplied path is a request to act
with borrowed privilege. The pattern that works is the one here: resolve the
path in the launcher as the calling user, pass *content* (not a name) across the
boundary, and have the Python refuse the flag if it ever arrives in a
service-user process. A flag that cannot be expressed that way probably should
not exist in hardened mode.

## Store migration: validated before any state exists

`do_install` runs `validate_store` over **both** existing stores before it
creates a service user, a venv, a sudoers file, or anything else. A store that
cannot be safely migrated must fail while the failure is still free; aborting
halfway through would leave a machine with a service user, a sudo rule, and no
credentials — a mess a user has to hand-unpick.

Validation and the copy share one walker (`_each_store_file`), so the thing that
was checked is exactly the thing that moves, and both passes apply the checks —
the copy is not trusting the earlier verdict. It refuses anything that is not a
plain regular file: a symlink planted in `~/.execute-db/` would otherwise
redirect a **root-run** `install -m 0600 -o <svc>` and let an unprivileged user
have root copy an arbitrary file into a store they can then read back through the
tool. Hard links (`nlink > 1`) are refused for the same reason in reverse: a link
the caller retains into a migrated file would survive the move.

Skipped entries and why: `SYSTEM` is the redirect marker (recreated on the user
side afterwards); `.ephemeral/` holds tokens whose keyring shares are bound to
the old uid and are meaningless after the move (see `EPHEMERAL_TOKENS.md`);
`cache/` holds regenerable schema documents and is a directory besides (see
`SCHEMA_INTROSPECTION.md`); `config.json` is a dead legacy index; `*.tmp` are
interrupted writes.

Plaintext env files migrate fine. Hardening and encryption answer different
questions — after the move the file is unreadable to your account either way,
and encryption is about whether *using* it prompts you. Requiring encryption
here would only have taught users to type a password they'd already decided they
didn't want.

## The shorter leash on token TTLs

`MAX_SYSTEM_TTL_SECONDS` (24h) caps `--ttl` when `in_system_mode()`. The reason
is specific, not a general "hardened means stricter": in the plain install a
token's keyring share lives in the user keyring (`@u`), which the kernel reaps
when the uid's last process exits — logout or reboot silently bounds every
token's real lifetime. In system mode there is no session to end, and separate
`sudo` invocations would lose the share between runs, so shares are anchored in
the service user's **persistent** keyring instead (`keyring._anchor`). That is
necessary for tokens to work at all here, and it removes the implicit bound. The
cap puts an explicit one back.

It is enforced in `tokens.parse_ttl`, inside the service-user process — the one
place the caller cannot reach around.

## Installing from a moving branch

Source selection: an explicit `EXECUTE_DB_REPO` wins; otherwise a local checkout
is used, but *only* when `$0` is a real file sitting next to a `pyproject.toml`
(so `curl | sudo bash` can never be tricked into "finding" one); otherwise the
canonical public repo. The default ref is `main`.

So the default install runs `pip` **as root** against a moving branch of a public
repo. This is trust-on-upgrade, and it is the largest piece of unmodelled trust
in the whole scheme — everything above is defending against your own account
while the install itself hands root to whatever `main` says today. `--ref <sha>`
pins a reviewed commit; the README says to use it. Re-running is idempotent and
is how you upgrade, which means re-running is also how you re-take that bet.

## Uninstall

`--uninstall` reverses each app: stops and removes the sweep units, removes the
sudoers rule and launcher, copies the store's top-level regular files back to
`~/.<name>` owned by you, drops the `SYSTEM` marker, deletes the service user,
and removes its home. Tokens (`.ephemeral/`) and `cache/` are not restored;
neither survives the uid change meaningfully.

The restore is gated on knowing the target user (`--user`, or `SUDO_USER`). If it
is unknown the loop still does `rm -rf` on the service home — so an uninstall run
as real root without `--user` destroys the stores. Anyone touching that path
should fix it rather than reproduce it.

## Residual risk

Stated plainly, so a reader does not over-read the guarantee:

- **The redirect is not a boundary.** Covered above. The trusted absolute path is
  the property; the marker is a hint.
- **Use is not confined, possession is.** `NOPASSWD` plus a plaintext env means
  anything running as you can invoke the launcher and run queries — read/write
  for `execute-db`. It cannot take the credential elsewhere. Encrypt the env
  (`password set`) if you want a per-use gate.
- **Password capture in your session** still works against anything that can read
  your terminal. Nothing on this side of the boundary fixes that.
- **Root and memory.** Root, or a debugger attached to the service-user process,
  sees the decrypted URL.
- **Installer trust.** See above.

The permanent backstop is unchanged and lives server-side: rotate the database
password, or issue roles with `VALID UNTIL`. Client-side machinery cannot revoke
knowledge.

## See also

- `ERROR_DISCLOSURE.md` — what a service-user process may say when a query
  fails. Closely coupled to the `-f` refusal above.
- `CREDENTIAL_STORE.md` — the on-disk format of what gets migrated.
- `EPHEMERAL_TOKENS.md` — tokens, key shares, and sweeping.
- `SCHEMA_INTROSPECTION.md` — the `cache/` directory this install skips.
