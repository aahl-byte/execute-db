#!/usr/bin/env bash
#
# execute-db / explore-db hardened installer — privilege separation ("system mode").
#
# Installs BOTH command-line tools with privilege separation:
#   execute-db  — read/write SQL
#   explore-db  — read-only SQL (the server rejects any write)
#
# Each tool's encrypted credential store moves under its OWN dedicated service
# user, unreadable to your account; you invoke each via a root-owned launcher and
# a locked-down sudoers rule. This matters for explore-db as much as execute-db:
# it holds the SAME database credentials, so if its store were left readable an
# agent running as you could read the connection string and connect read/write
# directly — defeating the whole point. Keeping the two stores separate also
# scopes ephemeral tokens to their tool: an explore-db (read-only) token is not
# accepted by execute-db, so read-only access you delegate can't be escalated to
# writes.
#
# Both tools share one root-owned venv (the execute-db package provides both
# scripts). Other processes running as you cannot read either secret store, read
# the kernel keyring shares, or tamper with the code you type your password into.
#
# Usage (run as root):
#   curl -fsSL https://raw.githubusercontent.com/aahl-byte/execute-db/main/install.sh | sudo bash
#   sudo ./install.sh [--ref <git-sha>] [--user <name>]
#   sudo ./install.sh --uninstall
#
# Re-running is idempotent (upgrade in place). Pin --ref to a commit SHA you
# trust: the install runs pip (as root) against that ref from a public repo.

set -euo pipefail

# The two front-ends installed from the one execute-db distribution. Everything
# below is derived from these names (service user, home, store, launcher, units).
APPS="execute-db explore-db"

LIB_DIR="/usr/local/lib/db-cli"   # shared, root-owned venv hosting both scripts
VENV="${LIB_DIR}/venv"
UNIT_DIR="/etc/systemd/system"

# Where to pip-install the package from, in priority order:
#   1. EXECUTE_DB_REPO set          -> git+${EXECUTE_DB_REPO}@${REF}
#   2. run from a local checkout    -> that directory (local testing/dev)
#   3. otherwise                    -> the canonical public repo (curl|bash)
# Pin a trusted commit SHA with --ref/EXECUTE_DB_REF for the git paths; the
# default branch is trust-on-upgrade.
REPO_EXPLICIT="${EXECUTE_DB_REPO:-}"
REPO="${EXECUTE_DB_REPO:-https://github.com/aahl-byte/execute-db}"
REF="${EXECUTE_DB_REF:-main}"

# Detect a local checkout: only when this script is a real file (not curl|bash)
# sitting next to a pyproject.toml.
LOCAL_SRC=""
if [ -f "$0" ]; then
    _self_dir="$(cd "$(dirname "$0")" && pwd)"
    [ -f "${_self_dir}/pyproject.toml" ] && LOCAL_SRC="$_self_dir"
fi

MODE="install"
TARGET_USER="${SUDO_USER:-}"

# Set per-app during the install/uninstall loops (used by the store helpers).
CUR_APP=""
MIG_SVC=""
MIG_STORE=""

die() { echo "error: $*" >&2; exit 1; }
info() { echo ">> $*"; }

[ "$(id -u)" -eq 0 ] || die "must run as root (use sudo)"

while [ $# -gt 0 ]; do
    case "$1" in
        --uninstall) MODE="uninstall" ;;
        --ref) shift; REF="${1:?--ref needs a value}" ;;
        --ref=*) REF="${1#*=}" ;;
        --user) shift; TARGET_USER="${1:?--user needs a value}" ;;
        --user=*) TARGET_USER="${1#*=}" ;;
        -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "unknown argument: $1" ;;
    esac
    shift
done

user_home() {
    getent passwd "$1" | cut -d: -f6
}

# Per-app naming, all derived from the app name (matches db_core.app.AppSpec):
#   execute-db -> user executedb, home /var/lib/execute-db, store .../.execute-db
app_user()      { printf '%s' "${1//-/}"; }
app_home()      { printf '/var/lib/%s' "$1"; }
app_store()     { printf '/var/lib/%s/.%s' "$1" "$1"; }
app_cli()       { printf '%s/bin/%s' "$VENV" "$1"; }
app_launcher()  { printf '/usr/local/bin/%s' "$1"; }
app_sudoers()   { printf '/etc/sudoers.d/%s' "$1"; }
app_sweep_svc() { printf '%s/%s-sweep.service' "$UNIT_DIR" "$1"; }
app_sweep_tmr() { printf '%s/%s-sweep.timer' "$UNIT_DIR" "$1"; }

# ---------------------------------------------------------------------------
# store migration / validation (shared by install & the pre-flight check)
# ---------------------------------------------------------------------------
# Walk the store applying `fn` to each migratable regular file. Shared by the
# early pre-flight validation and the actual copy so both agree on what moves.
# CUR_APP names the tool (for the "encrypt it first" hint).
_each_store_file() {
    local src="$1" fn="$2" entry base nlink
    for entry in "$src"/* "$src"/.env*; do
        [ -e "$entry" ] || continue
        base="$(basename "$entry")"
        case "$base" in
            SYSTEM|.ephemeral) continue ;;   # marker / uid-bound tokens: don't migrate
            cache) continue ;;               # schema documents: a dir, and regenerable
            config.json) continue ;;         # legacy index, no longer used
            *.tmp) continue ;;
        esac
        # Reject anything that isn't a plain regular file (symlink/fifo/dir/dev):
        # a pre-planted symlink here could otherwise redirect a root copy.
        if [ -L "$entry" ] || [ ! -f "$entry" ]; then
            die "refusing to migrate non-regular file: ${entry}"
        fi
        nlink="$(stat -c '%h' "$entry")"
        [ "$nlink" -eq 1 ] || die "refusing to migrate hard-linked file: ${entry}"
        # Plaintext and encrypted envs are both allowed. A plaintext env has no
        # per-use password gate even under hardening; encrypt it with
        # `${CUR_APP} password set --<name>` if you want one.
        "$fn" "$entry" "$base"
    done
}

# Pre-flight: fail BEFORE any system state is created if the store is unsafe.
validate_store() {
    local src="$1"
    [ -d "$src" ] || return 0
    _each_store_file "$src" true
}

_copy_one() {
    install -m 0600 -o "$MIG_SVC" -g "$MIG_SVC" "$1" "${MIG_STORE}/${2}"
}

migrate_store() {
    local src="$1" svc="$2" store="$3"
    [ -d "$src" ] || { info "[${CUR_APP}] No existing store at ${src}; starting fresh"; return; }
    info "[${CUR_APP}] Migrating credential store from ${src}"
    install -d -m 0700 -o "$svc" -g "$svc" "$store"
    MIG_SVC="$svc"; MIG_STORE="$store"
    _each_store_file "$src" _copy_one
}

# ---------------------------------------------------------------------------
# per-app resource installers
# ---------------------------------------------------------------------------
install_launcher() {
    local name="$1" svc="$2" cli="$3" launcher="$4"
    cat > "$launcher" <<LAUNCH
#!/bin/sh
# ${name} trusted launcher (root-owned). Re-execs the frozen CLI as ${svc}.
# If -f/--file is given, the file is read HERE as the calling user and piped in,
# so the service process never opens caller-named paths.
set -eu
VENV_CLI="${cli}"
FILE=""
ARGS=""
add() { ARGS="\${ARGS} \$(printf '%s' "\$1" | sed "s/'/'\\\\\\\\''/g; 1s/^/'/; \\\$s/\\\$/'/")"; }
while [ \$# -gt 0 ]; do
    case "\$1" in
        -f|--file) shift; FILE="\${1:-}" ;;
        --file=*) FILE="\${1#*=}" ;;
        -f*) FILE="\${1#-f}" ;;
        *) add "\$1" ;;
    esac
    shift
done
if [ -n "\$FILE" ]; then
    [ -r "\$FILE" ] || { echo "cannot read file: \$FILE" >&2; exit 1; }
    eval "set -- \$ARGS"
    exec sudo -H -u ${svc} -- "\$VENV_CLI" "\$@" < "\$FILE"
fi
eval "set -- \$ARGS"
exec sudo -H -u ${svc} -- "\$VENV_CLI" "\$@"
LAUNCH
    chown root:root "$launcher"
    chmod 0755 "$launcher"
}

install_sudoers() {
    local user="$1" svc="$2" cli="$3" sudoers="$4"
    local tmp_sudo
    tmp_sudo="$(mktemp)"
    cat > "$tmp_sudo" <<SUDO
# Managed by db-cli install.sh. Lets ${user} run ONLY the frozen CLI
# as ${svc}, with a reset environment (no PYTHONPATH/LD_* injection).
Defaults!${cli} env_reset, always_set_home
${user} ALL=(${svc}) NOPASSWD: ${cli} *
SUDO
    visudo -cf "$tmp_sudo" >/dev/null || { rm -f "$tmp_sudo"; die "generated sudoers failed validation"; }
    install -m 0440 -o root -g root "$tmp_sudo" "$sudoers"
    rm -f "$tmp_sudo"
}

install_sweep() {
    local name="$1" svc="$2" cli="$3" home="$4" svc_file="$5" tmr_file="$6"
    cat > "$svc_file" <<UNIT
[Unit]
Description=Wipe expired ${name} ephemeral tokens

[Service]
Type=oneshot
User=${svc}
ExecStart=${cli} token sweep
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
ReadWritePaths=${home}
CapabilityBoundingSet=
RestrictSUIDSGID=yes
LockPersonality=yes
ProtectKernelModules=yes
UNIT
    cat > "$tmr_file" <<UNIT
[Unit]
Description=Periodically wipe expired ${name} ephemeral tokens

[Timer]
OnBootSec=2min
OnUnitActiveSec=1min

[Install]
WantedBy=timers.target
UNIT
}

# ---------------------------------------------------------------------------
# uninstall
# ---------------------------------------------------------------------------
do_uninstall() {
    local uhome="" name svc home store launcher sudoers sweep_svc sweep_tmr restore
    [ -n "$TARGET_USER" ] && uhome="$(user_home "$TARGET_USER")"

    for name in $APPS; do
        svc="$(app_user "$name")"
        home="$(app_home "$name")"
        store="$(app_store "$name")"
        launcher="$(app_launcher "$name")"
        sudoers="$(app_sudoers "$name")"
        sweep_svc="$(app_sweep_svc "$name")"
        sweep_tmr="$(app_sweep_tmr "$name")"

        info "[${name}] Stopping and removing systemd units"
        systemctl disable --now "${name}-sweep.timer" 2>/dev/null || true
        rm -f "$sweep_svc" "$sweep_tmr"

        info "[${name}] Removing sudoers rule and launcher"
        rm -f "$sudoers" "$launcher"

        if [ -n "$TARGET_USER" ] && [ -d "$store" ]; then
            restore="${uhome}/.${name}"
            info "[${name}] Restoring credential store to ${restore}"
            install -d -m 0700 -o "$TARGET_USER" -g "$TARGET_USER" "$restore"
            # copy contents back (regular files only), leave ownership to the user
            find "$store" -mindepth 1 -maxdepth 1 -type f -exec cp -p {} "$restore"/ \;
            chown -R "$TARGET_USER":"$TARGET_USER" "$restore"
            rm -f "${restore}/SYSTEM"
        fi
        info "[${name}] Removing service user and home"
        userdel "$svc" 2>/dev/null || true
        rm -rf "$home"
    done

    systemctl daemon-reload 2>/dev/null || true
    info "Removing shared CLI venv ${LIB_DIR}"
    rm -rf "$LIB_DIR"
    info "Uninstalled.${TARGET_USER:+ Restored stores under $(user_home "$TARGET_USER")/.execute-db and /.explore-db}"
}

# ---------------------------------------------------------------------------
# install
# ---------------------------------------------------------------------------
print_done() {
    cat <<DONE

Hardened install complete — both tools are privilege-separated.

  execute-db (read/write)  store $(app_store execute-db)   launcher $(app_launcher execute-db)
  explore-db (read-only)   store $(app_store explore-db)   launcher $(app_launcher explore-db)
  Shared CLI venv (root-owned): ${VENV}

Each store is owned by its own service user and is unreadable to ${TARGET_USER}.

IMPORTANT — always invoke each tool by its trusted absolute path, not whatever
'execute-db'/'explore-db' your PATH resolves, so an agent cannot shadow it to
capture your password:

  $(app_launcher execute-db) --dev "UPDATE ..."
  $(app_launcher explore-db) --dev "SELECT ..."

Because the stores are separate, an explore-db (read-only) token is NOT accepted
by execute-db — read-only access you delegate cannot be escalated to writes.

Each tool keeps its own environments. A password at 'config set' is optional
(encrypts the env for a per-use password gate); plaintext envs work too and run
without a prompt:

  $(app_launcher execute-db) config set prod
  $(app_launcher explore-db) config set prod

To reverse everything:

  sudo $0 --uninstall
DONE
}

do_install() {
    [ -n "$TARGET_USER" ] || die "cannot determine the target user; pass --user <name>"
    id "$TARGET_USER" >/dev/null 2>&1 || die "user does not exist: ${TARGET_USER}"
    command -v python3 >/dev/null || die "python3 is required"

    local uhome name svc home store cli launcher sudoers sweep_svc sweep_tmr
    uhome="$(user_home "$TARGET_USER")"

    # Validate BOTH existing stores up front so a symlink or unencrypted env
    # aborts before we create any service user, venv, sudoers, etc.
    for name in $APPS; do
        CUR_APP="$name"
        validate_store "${uhome}/.${name}"
    done

    local source
    if [ -n "$REPO_EXPLICIT" ]; then
        source="git+${REPO}@${REF}"
    elif [ -n "$LOCAL_SRC" ]; then
        source="$LOCAL_SRC"
    else
        source="git+${REPO}@${REF}"
    fi

    info "Installing frozen CLIs into ${VENV} (source: ${source})"
    rm -rf "$LIB_DIR"
    install -d -m 0755 "$LIB_DIR"
    python3 -m venv "$VENV"
    "${VENV}/bin/pip" install --quiet --upgrade pip
    "${VENV}/bin/pip" install --quiet "$source"
    # Root-owned, not writable by anyone else — this is the code-tamper defense.
    chown -R root:root "$LIB_DIR"
    chmod -R go-w "$LIB_DIR"
    # Only real files/dirs matter; symlink perms are always 0777 and irrelevant.
    if find "$LIB_DIR" \( -type f -o -type d \) -perm /022 -print -quit | grep -q .; then
        die "sanity check failed: ${LIB_DIR} has group/other-writable files"
    fi

    for name in $APPS; do
        CUR_APP="$name"
        svc="$(app_user "$name")"
        home="$(app_home "$name")"
        store="$(app_store "$name")"
        cli="$(app_cli "$name")"
        launcher="$(app_launcher "$name")"
        sudoers="$(app_sudoers "$name")"
        sweep_svc="$(app_sweep_svc "$name")"
        sweep_tmr="$(app_sweep_tmr "$name")"

        [ -x "$cli" ] || die "install did not produce ${cli}"

        info "[${name}] Creating service user ${svc}"
        if ! id "$svc" >/dev/null 2>&1; then
            useradd --system --home-dir "$home" --shell /usr/sbin/nologin "$svc"
        fi
        install -d -m 0700 -o "$svc" -g "$svc" "$home"

        info "[${name}] Installing launcher ${launcher}"
        install_launcher "$name" "$svc" "$cli" "$launcher"

        info "[${name}] Installing sudoers rule ${sudoers}"
        install_sudoers "$TARGET_USER" "$svc" "$cli" "$sudoers"

        migrate_store "${uhome}/.${name}" "$svc" "$store"

        info "[${name}] Dropping redirect marker for ${TARGET_USER}"
        install -d -m 0700 -o "$TARGET_USER" -g "$TARGET_USER" "${uhome}/.${name}"
        : > "${uhome}/.${name}/SYSTEM"
        chown "$TARGET_USER":"$TARGET_USER" "${uhome}/.${name}/SYSTEM"

        info "[${name}] Installing systemd sweep timer"
        install_sweep "$name" "$svc" "$cli" "$home" "$sweep_svc" "$sweep_tmr"
    done

    if command -v systemctl >/dev/null 2>&1; then
        systemctl daemon-reload
        for name in $APPS; do
            systemctl enable --now "${name}-sweep.timer" >/dev/null 2>&1 || \
                info "note: could not enable the ${name}-sweep.timer"
        done
    else
        info "note: systemctl not found; sweep units written but not enabled"
    fi

    print_done
}

case "$MODE" in
    install)   do_install ;;
    uninstall) do_uninstall ;;
esac
