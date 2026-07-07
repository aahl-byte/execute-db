#!/usr/bin/env bash
#
# execute-db hardened installer — privilege separation ("system mode").
#
# Moves your encrypted credentials under a dedicated service user and installs
# a root-owned copy of the CLI that you invoke as that user via a locked-down
# sudoers rule. Other processes running as you (scripts, coding agents) then
# cannot read the secret files, read the kernel keyring share, or tamper with
# the code you type your password into.
#
# Usage (run as root):
#   curl -fsSL https://raw.githubusercontent.com/aahl-byte/execute-db/main/install.sh | sudo bash
#   sudo ./install.sh [--ref <git-sha>] [--user <name>]
#   sudo ./install.sh --uninstall
#
# Re-running is idempotent (upgrade in place). Pin --ref to a commit SHA you
# trust: the install runs pip (as root) against that ref from a public repo.

set -euo pipefail

SERVICE_USER="executedb"
SERVICE_HOME="/var/lib/execute-db"
STORE_DIR="${SERVICE_HOME}/.execute-db"
LIB_DIR="/usr/local/lib/execute-db"
VENV="${LIB_DIR}/venv"
VENV_CLI="${VENV}/bin/execute-db"
LAUNCHER="/usr/local/bin/execute-db"
SUDOERS="/etc/sudoers.d/execute-db"
UNIT_DIR="/etc/systemd/system"
SWEEP_SERVICE="${UNIT_DIR}/execute-db-sweep.service"
SWEEP_TIMER="${UNIT_DIR}/execute-db-sweep.timer"

# REPO is overridable (EXECUTE_DB_REPO) only to allow install testing against a
# local checkout; the default is the canonical public repo.
REPO="${EXECUTE_DB_REPO:-https://github.com/aahl-byte/execute-db}"
# Pin to a trusted commit SHA. Override with --ref or EXECUTE_DB_REF. Default is
# a branch (trust-on-upgrade); pinning a SHA is strongly recommended.
REF="${EXECUTE_DB_REF:-main}"

MODE="install"
TARGET_USER="${SUDO_USER:-}"

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

# ---------------------------------------------------------------------------
# uninstall
# ---------------------------------------------------------------------------
do_uninstall() {
    info "Stopping and removing systemd units"
    systemctl disable --now execute-db-sweep.timer 2>/dev/null || true
    rm -f "$SWEEP_SERVICE" "$SWEEP_TIMER"
    systemctl daemon-reload 2>/dev/null || true

    info "Removing sudoers rule and launcher"
    rm -f "$SUDOERS" "$LAUNCHER"

    info "Removing installed CLI"
    rm -rf "$LIB_DIR"

    if [ -n "$TARGET_USER" ] && [ -d "$STORE_DIR" ]; then
        local home restore
        home="$(user_home "$TARGET_USER")"
        restore="${home}/.execute-db"
        info "Restoring credential store to ${restore}"
        install -d -m 0700 -o "$TARGET_USER" -g "$TARGET_USER" "$restore"
        # copy contents back (regular files only), leave ownership to the user
        find "$STORE_DIR" -mindepth 1 -maxdepth 1 -type f -exec cp -p {} "$restore"/ \;
        chown -R "$TARGET_USER":"$TARGET_USER" "$restore"
        rm -f "${restore}/SYSTEM"
    fi
    info "Removing service user and home"
    userdel "$SERVICE_USER" 2>/dev/null || true
    rm -rf "$SERVICE_HOME"

    info "Uninstalled. (Your restored store is at ${TARGET_USER:+$(user_home "$TARGET_USER")/.execute-db})"
}

# ---------------------------------------------------------------------------
# install
# ---------------------------------------------------------------------------
# Walk the store applying `fn` to each migratable regular file. Shared by the
# early pre-flight validation and the actual copy so both agree on what moves.
_each_store_file() {
    local src="$1" fn="$2" entry base nlink
    for entry in "$src"/* "$src"/.env* "$src"/config.json; do
        [ -e "$entry" ] || continue
        base="$(basename "$entry")"
        case "$base" in
            SYSTEM|.ephemeral) continue ;;   # marker / uid-bound tokens: don't migrate
            *.tmp) continue ;;
        esac
        # Reject anything that isn't a plain regular file (symlink/fifo/dir/dev):
        # a pre-planted symlink here could otherwise redirect a root copy.
        if [ -L "$entry" ] || [ ! -f "$entry" ]; then
            die "refusing to migrate non-regular file: ${entry}"
        fi
        nlink="$(stat -c '%h' "$entry")"
        [ "$nlink" -eq 1 ] || die "refusing to migrate hard-linked file: ${entry}"
        # Every .env* must be encrypted (magic 'EXDB1'); system mode has no
        # password gate for plaintext, so refuse to complete otherwise.
        case "$base" in
            .env|.env.*)
                local magic
                magic="$(head -c5 "$entry" 2>/dev/null || true)"
                [ "$magic" = "EXDB1" ] || die \
                    "environment file ${base} is not encrypted. Encrypt it first: execute-db password set --${base#.env.}"
                ;;
        esac
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
    install -m 0600 -o "$SERVICE_USER" -g "$SERVICE_USER" "$1" "${STORE_DIR}/${2}"
}

migrate_store() {
    local src="$1"
    [ -d "$src" ] || { info "No existing store at ${src}; starting fresh"; return; }
    info "Migrating credential store from ${src}"
    install -d -m 0700 -o "$SERVICE_USER" -g "$SERVICE_USER" "$STORE_DIR"
    _each_store_file "$src" _copy_one
}

do_install() {
    [ -n "$TARGET_USER" ] || die "cannot determine the target user; pass --user <name>"
    id "$TARGET_USER" >/dev/null 2>&1 || die "user does not exist: ${TARGET_USER}"
    command -v python3 >/dev/null || die "python3 is required"

    # Validate the existing store up front so a symlink or unencrypted env
    # aborts before we create the service user, venv, sudoers, etc.
    validate_store "$(user_home "$TARGET_USER")/.execute-db"

    info "Creating service user ${SERVICE_USER}"
    if ! id "$SERVICE_USER" >/dev/null 2>&1; then
        useradd --system --home-dir "$SERVICE_HOME" --shell /usr/sbin/nologin "$SERVICE_USER"
    fi
    install -d -m 0700 -o "$SERVICE_USER" -g "$SERVICE_USER" "$SERVICE_HOME"

    info "Installing frozen CLI into ${VENV} (ref: ${REF})"
    rm -rf "$LIB_DIR"
    install -d -m 0755 "$LIB_DIR"
    python3 -m venv "$VENV"
    "${VENV}/bin/pip" install --quiet --upgrade pip
    "${VENV}/bin/pip" install --quiet "git+${REPO}@${REF}"
    # Root-owned, not writable by anyone else — this is the code-tamper defense.
    chown -R root:root "$LIB_DIR"
    chmod -R go-w "$LIB_DIR"
    # Only real files/dirs matter; symlink perms are always 0777 and irrelevant.
    if find "$LIB_DIR" \( -type f -o -type d \) -perm /022 -print -quit | grep -q .; then
        die "sanity check failed: ${LIB_DIR} has group/other-writable files"
    fi
    [ -x "$VENV_CLI" ] || die "install did not produce ${VENV_CLI}"

    info "Installing launcher ${LAUNCHER}"
    cat > "$LAUNCHER" <<LAUNCH
#!/bin/sh
# execute-db trusted launcher (root-owned). Re-execs the frozen CLI as the
# service user. If -f/--file is given, the file is read HERE as the calling
# user and piped in, so the service process never opens caller-named paths.
set -eu
VENV_CLI="${VENV_CLI}"
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
    exec sudo -H -u ${SERVICE_USER} -- "\$VENV_CLI" "\$@" < "\$FILE"
fi
eval "set -- \$ARGS"
exec sudo -H -u ${SERVICE_USER} -- "\$VENV_CLI" "\$@"
LAUNCH
    chown root:root "$LAUNCHER"
    chmod 0755 "$LAUNCHER"

    info "Installing sudoers rule ${SUDOERS}"
    local tmp_sudo
    tmp_sudo="$(mktemp)"
    cat > "$tmp_sudo" <<SUDO
# Managed by execute-db install.sh. Lets ${TARGET_USER} run ONLY the frozen CLI
# as ${SERVICE_USER}, with a reset environment (no PYTHONPATH/LD_* injection).
Defaults!${VENV_CLI} env_reset, always_set_home
${TARGET_USER} ALL=(${SERVICE_USER}) NOPASSWD: ${VENV_CLI} *
SUDO
    visudo -cf "$tmp_sudo" >/dev/null || { rm -f "$tmp_sudo"; die "generated sudoers failed validation"; }
    install -m 0440 -o root -g root "$tmp_sudo" "$SUDOERS"
    rm -f "$tmp_sudo"

    migrate_store "$(user_home "$TARGET_USER")/.execute-db"

    info "Dropping redirect marker for ${TARGET_USER}"
    local umark
    umark="$(user_home "$TARGET_USER")/.execute-db"
    install -d -m 0700 -o "$TARGET_USER" -g "$TARGET_USER" "$umark"
    : > "${umark}/SYSTEM"
    chown "$TARGET_USER":"$TARGET_USER" "${umark}/SYSTEM"

    info "Installing systemd sweep timer"
    cat > "$SWEEP_SERVICE" <<UNIT
[Unit]
Description=Wipe expired execute-db ephemeral tokens

[Service]
Type=oneshot
User=${SERVICE_USER}
ExecStart=${VENV_CLI} token sweep
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
ReadWritePaths=${SERVICE_HOME}
CapabilityBoundingSet=
RestrictSUIDSGID=yes
LockPersonality=yes
ProtectKernelModules=yes
UNIT
    cat > "$SWEEP_TIMER" <<UNIT
[Unit]
Description=Periodically wipe expired execute-db ephemeral tokens

[Timer]
OnBootSec=2min
OnUnitActiveSec=1min

[Install]
WantedBy=timers.target
UNIT
    if command -v systemctl >/dev/null 2>&1; then
        systemctl daemon-reload
        systemctl enable --now execute-db-sweep.timer >/dev/null 2>&1 || \
            info "note: could not enable the sweep timer"
    else
        info "note: systemctl not found; sweep units written but not enabled"
    fi

    cat <<DONE

Hardened install complete.

  Secrets:   ${STORE_DIR} (owned by ${SERVICE_USER}, unreadable to ${TARGET_USER})
  CLI:       ${VENV_CLI} (root-owned)
  Launcher:  ${LAUNCHER}

IMPORTANT — to keep an agent from capturing your password, always invoke the
tool by its trusted absolute path, not whatever 'execute-db' your PATH resolves:

  ${LAUNCHER} --dev "SELECT 1"

Consider a root-owned shell alias for convenience. To reverse everything:

  sudo $0 --uninstall
DONE
}

case "$MODE" in
    install)   do_install ;;
    uninstall) do_uninstall ;;
esac
