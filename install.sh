#!/bin/sh

# Project: codex-tier-routing
# Original project by: 0xTitanas
# Canonical source: https://github.com/0xTitanas/codex-tier-routing
# Copyright: 2026 0xTitanas
# SPDX-License-Identifier: MIT

set -eu

# Tests rewrite this literal in a temporary copy. Environment variables cannot
# activate installer fault or command seams in the shipped script.
INSTALL_TEST_BUILD=0

# The invoking shell has already started, but no child process should inherit
# Python startup hooks or dynamic-loader injection variables from the caller.
unset PYTHONHOME PYTHONPATH PYTHONSTARTUP PYTHONINSPECT PYTHONUSERBASE
unset LD_PRELOAD LD_LIBRARY_PATH LD_AUDIT
unset DYLD_INSERT_LIBRARIES DYLD_LIBRARY_PATH DYLD_FRAMEWORK_PATH
unset DYLD_FALLBACK_LIBRARY_PATH DYLD_FALLBACK_FRAMEWORK_PATH

ORIGINAL_PATH=${PATH:-}
SAFE_PATH=$(command -p getconf PATH 2>/dev/null || printf '/usr/bin:/bin:/usr/sbin:/sbin')
PATH=$SAFE_PATH
export PATH

usage() {
  cat <<'EOF'
Usage: ./install.sh [--dry-run] [--strict] [--codex-home PATH]

Install codex-tier-routing artifacts and the local dispatcher.

Options:
  --dry-run    Show actions without modifying the filesystem.
  --strict     Disable native Codex spawning so routed delegation is exclusive.
  --codex-home Install into an explicit absolute Codex home owned by this account.
  --help       Show this help message.

DESTINATION uses $HOME/.codex only when neither --codex-home nor CODEX_HOME
is present. A present empty, tilde, dot, or relative value is rejected.
EOF
}

error() {
  printf '%s\n' "$1" >&2
  exit 1
}

RM_BIN=$(command -v rm 2>/dev/null || printf '')
case "$RM_BIN" in
  /*) ;;
  *) error "Unable to locate the system rm utility" ;;
esac

DRY_RUN=0
STRICT=0
EXPLICIT_CODEX_HOME=
EXPLICIT_CODEX_HOME_PRESENT=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --dry-run)
      DRY_RUN=1
      ;;
    --strict)
      STRICT=1
      ;;
    --codex-home)
      EXPLICIT_CODEX_HOME_PRESENT=1
      shift
      [ "$#" -gt 0 ] || error "--codex-home requires a path"
      EXPLICIT_CODEX_HOME=$1
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      error "Unknown option: $1"
      ;;
  esac
  shift
 done

script_path=$0
case "$script_path" in
  */*)
    script_dir=${script_path%/*}
    ;;
  *)
    script_dir=.
    ;;
esac
SCRIPT_DIR=$(CDPATH= cd -P "$script_dir" && pwd)
REPO_ROOT=$SCRIPT_DIR

if [ "$EXPLICIT_CODEX_HOME_PRESENT" -eq 1 ]; then
  CODEX_HOME=$EXPLICIT_CODEX_HOME
elif [ "${CODEX_HOME+x}" = x ]; then
  CODEX_HOME=$CODEX_HOME
else
  CODEX_HOME=$HOME/.codex
fi
case "$CODEX_HOME" in
  /*) ;;
  *) error "Codex home must be an absolute path: $CODEX_HOME" ;;
esac
AGENTS_SRC="$REPO_ROOT/agents"
SKILL_SRC="$REPO_ROOT/skills/model-routing"
DISPATCHER_SRC="$REPO_ROOT/bin/codex-tier"
DISPATCHER_RUNTIME_SRC="$REPO_ROOT/bin/codex-tier.py"
AGENTS_DST="$CODEX_HOME/agents"
SKILL_DST="$CODEX_HOME/skills/model-routing"
DISPATCHER_DST="$CODEX_HOME/bin/codex-tier"
DISPATCHER_RUNTIME_DST="$CODEX_HOME/bin/codex-tier.py"

if [ "$DRY_RUN" -eq 1 ]; then
  echo "Dry run enabled: no filesystem changes will be made."
fi

host_uid=$(id -u 2>/dev/null || printf '')
case "$host_uid" in
  ''|*[!0-9]*) error "Unable to determine numeric effective UID" ;;
esac
if [ "$INSTALL_TEST_BUILD" -eq 1 ] && [ "${CODEX_TIER_INSTALL_TEST_SEAM:-}" = 1 ] && [ "$host_uid" != 0 ] && [ -n "${CODEX_TIER_INSTALL_TEST_PATH:-}" ]; then
  case "$CODEX_TIER_INSTALL_TEST_PATH" in
    /*) PATH="$CODEX_TIER_INSTALL_TEST_PATH:$SAFE_PATH" ;;
    *) error "Test utility path must be absolute" ;;
  esac
  export PATH
fi
effective_uid=$(id -u 2>/dev/null || printf '')
case "$effective_uid" in
  ''|*[!0-9]*) error "Unable to determine numeric effective UID" ;;
esac
echo "Effective install UID: $effective_uid"
echo "Codex home: $CODEX_HOME"

platform=$(uname -s 2>/dev/null || printf '')
case "$platform" in
  Darwin)
    owner_uid() { stat -f '%u' "$1" 2>/dev/null || printf ''; }
    device_id() { stat -f '%d' "$1" 2>/dev/null || printf ''; }
    # %Lp omits the special permission digit on Darwin.  Prefixing %Mp keeps
    # sticky/set-id bits so a real 01777 boundary is distinguishable from 0777.
    permission_mode() { stat -f '%Mp%Lp' "$1" 2>/dev/null || printf ''; }
    ;;
  Linux)
    owner_uid() { stat -c '%u' "$1" 2>/dev/null || printf ''; }
    device_id() { stat -c '%d' "$1" 2>/dev/null || printf ''; }
    permission_mode() { stat -c '%a' "$1" 2>/dev/null || printf ''; }
    ;;
  *)
    error "Unable to determine supported platform for install boundary ownership"
    ;;
esac

validate_owned_existing() {
  owned_path=$1
  owned_label=$2
  if [ -L "$owned_path" ]; then
    error "Refusing symlinked $owned_label: $owned_path"
  fi
  [ -e "$owned_path" ] || return 0
  owned_uid=$(owner_uid "$owned_path")
  case "$owned_uid" in
    ''|*[!0-9]*) error "Unable to determine numeric ownership for $owned_label: $owned_path" ;;
  esac
  if [ "$owned_uid" != "$effective_uid" ]; then
    error "Refusing cross-owner $owned_label: $owned_path is owned by UID $owned_uid, current UID is $effective_uid"
  fi
}

validate_darwin_acl() {
  [ "$platform" = Darwin ] || return 0
  # `ls -lde` includes ACL entries after the mode line.  Deny-only entries are
  # normal on macOS homes; any allow entry grants a principal outside this
  # pathname contract and is refused.  A failed probe is not evidence of safety.
  acl_output=$(ls -lde "$1" 2>/dev/null) || error "Unable to inspect ACLs for install boundary: $1"
  if printf '%s\n' "$acl_output" | grep -Eq '^[[:space:]]*[0-9]+:.*[[:space:]]allow[[:space:]]'; then
    error "Refusing ACL allow entry on install boundary: $1"
  fi
}

validate_chain_components() {
  chain_path=$1
  chain_label=$2
  case "$chain_path" in
    /*) ;;
    *) error "$chain_label must be absolute: $chain_path" ;;
  esac

  chain_root_uid=$(owner_uid /)
  [ "$chain_root_uid" = 0 ] || error "Refusing non-root-owned $chain_label root"
  chain_root_mode=$(permission_mode /)
  case "$chain_root_mode" in
    [0-7][0-7][0-7]|[0-7][0-7][0-7][0-7]) ;;
    *) error "Unable to determine $chain_label root permissions" ;;
  esac
  chain_root_without_other=${chain_root_mode%?}
  chain_root_group=${chain_root_without_other#${chain_root_without_other%?}}
  chain_root_other=${chain_root_mode#${chain_root_mode%?}}
  case "$chain_root_group:$chain_root_other" in
    [2367]:*|*:[2367]) error "Refusing writable $chain_label root" ;;
  esac
  validate_darwin_acl /

  chain_current=/
  chain_rest=${chain_path#/}
  while [ -n "$chain_rest" ]; do
    chain_component=${chain_rest%%/*}
    case "$chain_component" in
      ''|.|..) error "$chain_label contains an unsafe lexical component: $chain_path" ;;
    esac
    if [ "$chain_current" = / ]; then
      chain_current=/$chain_component
    else
      chain_current=$chain_current/$chain_component
    fi
    [ -e "$chain_current" ] || [ -L "$chain_current" ] || error "Missing $chain_label component: $chain_current"
    chain_uid=$(owner_uid "$chain_current")
    case "$chain_uid" in
      ''|*[!0-9]*) error "Unable to determine $chain_label ownership: $chain_current" ;;
    esac
    if [ "$chain_uid" != 0 ] && [ "$chain_uid" != "$effective_uid" ]; then
      error "Refusing foreign-owned $chain_label component: $chain_current"
    fi
    if [ ! -L "$chain_current" ]; then
      chain_mode=$(permission_mode "$chain_current")
      case "$chain_mode" in
        [0-7][0-7][0-7]|[0-7][0-7][0-7][0-7]) ;;
        *) error "Unable to determine $chain_label permissions: $chain_current" ;;
      esac
      chain_without_other=${chain_mode%?}
      chain_group=${chain_without_other#${chain_without_other%?}}
      chain_other=${chain_mode#${chain_mode%?}}
      case "$chain_other" in
        [2367]) error "Refusing world-writable $chain_label component: $chain_current" ;;
      esac
      if [ "$chain_uid" != "$effective_uid" ]; then
        case "$chain_group" in
          [2367]) error "Refusing group-writable foreign $chain_label component: $chain_current" ;;
        esac
      fi
      validate_darwin_acl "$chain_current"
    fi
    [ "$chain_component" = "$chain_rest" ] && break
    chain_rest=${chain_rest#*/}
  done
}

validate_trusted_chain() {
  trusted_path=$1
  trusted_label=$2
  validate_chain_components "$trusted_path" "$trusted_label"
  trusted_resolved=$(realpath "$trusted_path" 2>/dev/null || printf '')
  case "$trusted_resolved" in
    /*) ;;
    *) error "Unable to resolve $trusted_label: $trusted_path" ;;
  esac
  validate_chain_components "$trusted_resolved" "resolved $trusted_label"
  printf '%s\n' "$trusted_resolved"
}

run_python() {
  if [ "${CODEX_TIER_CODEX_BIN+x}" = x ]; then
    "$ENV_BIN" -i \
      HOME="$HOME" \
      PATH="$ORIGINAL_PATH" \
      CODEX_HOME="$CODEX_HOME" \
      CODEX_TIER_CODEX_BIN="$CODEX_TIER_CODEX_BIN" \
      "$PYTHON_BIN" -I "$@"
  else
    "$ENV_BIN" -i \
      HOME="$HOME" \
      PATH="$ORIGINAL_PATH" \
      CODEX_HOME="$CODEX_HOME" \
      "$PYTHON_BIN" -I "$@"
  fi
}

run_dispatcher() {
  run_python "$DISPATCHER_RUNTIME_DST" "$@"
}

validate_runtime_path() {
  [ -n "${HOME:-}" ] || error "HOME is required for strict installation"
  ENV_BIN=$(PATH="$SAFE_PATH" command -v env 2>/dev/null || printf '')
  case "$ENV_BIN" in
    /*) ;;
    *) error "Unable to locate the system env utility" ;;
  esac

  if [ "$INSTALL_TEST_BUILD" -eq 1 ] && [ "${CODEX_TIER_INSTALL_TEST_SEAM:-}" = 1 ] && [ "$host_uid" != 0 ] && [ -n "${CODEX_TIER_INSTALL_TEST_PYTHON_BIN:-}" ]; then
    PYTHON_BIN=$CODEX_TIER_INSTALL_TEST_PYTHON_BIN
    case "$PYTHON_BIN" in
      /*) ;;
      *) error "Test Python path must be absolute" ;;
    esac
    [ -f "$PYTHON_BIN" ] && [ -x "$PYTHON_BIN" ] || error "Test Python is not an executable regular file: $PYTHON_BIN"
  else
    validate_trusted_chain "$ENV_BIN" "env executable path" >/dev/null
    [ -n "$ORIGINAL_PATH" ] || error "Runtime PATH is empty"
    PYTHON_BIN=${CODEX_TIER_PYTHON_BIN:-}
    if [ -n "$PYTHON_BIN" ]; then
      case "$PYTHON_BIN" in
        /*) ;;
        *) error "CODEX_TIER_PYTHON_BIN must be absolute" ;;
      esac
      PYTHON_BIN=$(validate_trusted_chain "$PYTHON_BIN" "Python 3 executable path")
      [ -f "$PYTHON_BIN" ] && [ -x "$PYTHON_BIN" ] || error "Python 3 is not an executable regular file: $PYTHON_BIN"
      run_python -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' || error "CODEX_TIER_PYTHON_BIN must be Python 3.11 or newer"
    else
      for python_name in python3.11 python3.12 python3.13 python3.14 python3.15 python3; do
        python_candidate=$(PATH="$ORIGINAL_PATH" command -v "$python_name" 2>/dev/null || printf '')
        [ -n "$python_candidate" ] || continue
        case "$python_candidate" in
          /*) ;;
          *) continue ;;
        esac
        python_candidate=$(validate_trusted_chain "$python_candidate" "Python 3 executable path")
        [ -f "$python_candidate" ] && [ -x "$python_candidate" ] || error "Python 3 is not an executable regular file: $python_candidate"
        PYTHON_BIN=$python_candidate
        if run_python -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
          break
        fi
        PYTHON_BIN=
      done
      [ -n "$PYTHON_BIN" ] || error "Python 3.11 or newer is unavailable from PATH"
    fi
  fi

  run_python -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' || error "Python 3.11 or newer is required"
}

validate_safe_existing_object() {
  safe_path=$1
  safe_label=$2
  validate_owned_existing "$safe_path" "$safe_label"
  [ -e "$safe_path" ] || return 0
  safe_mode=$(permission_mode "$safe_path")
  case "$safe_mode" in
    [0-7][0-7][0-7]|[0-7][0-7][0-7][0-7]) ;;
    *) error "Unable to determine numeric permissions for $safe_label: $safe_path" ;;
  esac
  safe_without_other=${safe_mode%?}
  safe_group=${safe_without_other#${safe_without_other%?}}
  safe_other=${safe_mode#${safe_mode%?}}
  case "$safe_group:$safe_other" in
    [2367]:*|*:[2367]) error "Refusing group/world-writable $safe_label: $safe_path" ;;
  esac
  validate_darwin_acl "$safe_path"
}

validate_private_object() {
  private_path=$1
  private_mode=$2
  private_label=$3
  validate_owned_existing "$private_path" "$private_label"
  [ -e "$private_path" ] || error "Missing $private_label after installation: $private_path"
  observed_private_mode=$(permission_mode "$private_path")
  case "$observed_private_mode" in
    "$private_mode"|"0$private_mode") ;;
    *) error "Unsafe mode on $private_label: $private_path" ;;
  esac
  validate_darwin_acl "$private_path"
}

normalize_private_tree() {
  private_tree=$1
  if [ "$platform" = Darwin ]; then
    chmod -RN "$private_tree" || error "Unable to remove inherited ACLs from private tree: $private_tree"
  fi
  find "$private_tree" -type d -exec chmod 700 {} + || error "Unable to secure private directories: $private_tree"
  find "$private_tree" -type f -exec chmod 600 {} + || error "Unable to secure private files: $private_tree"
}

validate_private_tree() {
  private_tree=$1
  private_tree_label=$2
  [ -d "$private_tree" ] && [ ! -L "$private_tree" ] || error "Unsafe $private_tree_label root: $private_tree"
  unexpected_node=$(find "$private_tree" \( -type l -o \( ! -type d ! -type f \) \) -print -quit)
  [ -z "$unexpected_node" ] || error "Unsafe node in $private_tree_label: $unexpected_node"
  if ! find "$private_tree" -exec sh -c '
    for candidate do
      [ -O "$candidate" ] || exit 1
    done
  ' sh {} +; then
    error "Cross-owner object in $private_tree_label: $private_tree"
  fi
  unsafe_directory=$(find "$private_tree" -type d ! -perm 700 -print -quit)
  [ -z "$unsafe_directory" ] || error "Unsafe directory mode in $private_tree_label: $unsafe_directory"
  unsafe_file=$(find "$private_tree" -type f ! -perm 600 -print -quit)
  [ -z "$unsafe_file" ] || error "Unsafe file mode in $private_tree_label: $unsafe_file"
  if [ "$platform" = Darwin ]; then
    if ! find "$private_tree" -exec sh -c '
      for candidate do
        acl_output=$(ls -lde "$candidate" 2>/dev/null) || exit 1
        if printf "%s\n" "$acl_output" | grep -Eq "^[[:space:]]*[0-9]+:.*[[:space:]]allow[[:space:]]"; then
          exit 1
        fi
      done
    ' sh {} +; then
      error "Unsafe or unreadable ACL in $private_tree_label: $private_tree"
    fi
  fi
}

# Validate the lexical pathname, not a resolved pathname.  `test -L` catches
# both live and dangling symlinks before stat is allowed to follow anything.
# The loop deliberately checks / as a component too.
validate_codex_home_chain() {
  # Do this as lexical component parsing rather than shell-pattern matching:
  # the latter is easy to get wrong for terminal and repeated-slash-adjacent
  # dot components.  Empty components are harmless repeated slashes.
  lexical_rest=${CODEX_HOME#/}
  while :; do
    lexical_component=${lexical_rest%%/*}
    case "$lexical_component" in
      .|..) error "Codex home must not contain . or .. components: $CODEX_HOME" ;;
    esac
    [ "$lexical_component" = "$lexical_rest" ] && break
    lexical_rest=${lexical_rest#*/}
  done
  chain_path=/
  previous_writable=0
  missing_seen=0
  CODEX_HOME_EXISTS=1
  chain_rest=${CODEX_HOME#/}
  chain_first=1
  while :; do
    if [ "$chain_first" -eq 1 ]; then
      chain_first=0
      chain_path=/
    elif [ -n "$chain_rest" ]; then
      chain_component=${chain_rest%%/*}
      if [ "$chain_component" = "$chain_rest" ]; then chain_rest=; else chain_rest=${chain_rest#*/}; fi
      [ -n "$chain_component" ] || continue
      [ "$chain_path" = / ] && chain_path="/$chain_component" || chain_path="$chain_path/$chain_component"
    else
      break
    fi
    if [ -L "$chain_path" ]; then
      error "Refusing symlinked Codex home component: $chain_path"
    fi
    if [ ! -e "$chain_path" ]; then
      missing_seen=1
      CODEX_HOME_EXISTS=0
      continue
    fi
    [ "$missing_seen" -eq 0 ] || error "Codex home has an unexpected existing component after an absent ancestor: $chain_path"
    [ -d "$chain_path" ] || error "Codex home component is not a directory: $chain_path"
    chain_uid=$(owner_uid "$chain_path")
    case "$chain_uid" in ''|*[!0-9]*) error "Unable to determine numeric ownership for Codex home component: $chain_path" ;; esac
    if [ "$chain_uid" != "$effective_uid" ] && [ "$chain_uid" != 0 ]; then
      error "Refusing foreign-owned Codex home component: $chain_path"
    fi
    validate_darwin_acl "$chain_path"
    chain_mode=$(permission_mode "$chain_path")
    case "$chain_mode" in [0-7][0-7][0-7]|[0-7][0-7][0-7][0-7]) ;; *) error "Unable to determine numeric permissions for Codex home component: $chain_path" ;; esac
    chain_without_other=${chain_mode%?}
    chain_group=${chain_without_other#${chain_without_other%?}}
    chain_other=${chain_mode#${chain_mode%?}}
    if [ "$previous_writable" -eq 1 ] && [ "$chain_uid" != "$effective_uid" ] && [ "$chain_uid" != 0 ]; then
      error "Refusing foreign-owned child beneath writable Codex home ancestor: $chain_path"
    fi
    case "$chain_group:$chain_other" in
      [2367]:*|*:[2367])
        case "$chain_mode" in [1357][0-7][0-7][0-7]) ;; *) error "Refusing non-sticky group/world-writable Codex home ancestor: $chain_path" ;; esac
        previous_writable=1
        ;;
      *) previous_writable=0 ;;
    esac
  done

  if [ "$CODEX_HOME_EXISTS" -eq 1 ]; then
    [ -L "$CODEX_HOME" ] && error "Refusing symlinked Codex home: $CODEX_HOME"
    home_uid=$(owner_uid "$CODEX_HOME")
    [ "$home_uid" = "$effective_uid" ] || error "Refusing cross-owner Codex home: $CODEX_HOME"
    home_mode=$(permission_mode "$CODEX_HOME")
    case "$home_mode" in [0-7][0-7][0-7]|[0-7][0-7][0-7][0-7]) ;; *) error "Unable to determine numeric permissions for Codex home: $CODEX_HOME" ;; esac
    home_without_other=${home_mode%?}; home_group=${home_without_other#${home_without_other%?}}; home_other=${home_mode#${home_mode%?}}
    case "$home_group:$home_other" in [2367]:*|*:[2367]) error "Refusing group/world-writable Codex home: $CODEX_HOME" ;; esac
  fi
}

ensure_codex_home() {
  while :; do
    validate_codex_home_chain
    [ "$CODEX_HOME_EXISTS" -eq 1 ] && return 0
    [ "$DRY_RUN" -eq 1 ] && return 0
    maybe_inject_home_creation_race
    if mkdir -m 700 "$CODEX_HOME" 2>/dev/null; then
      HOME_CREATED=1
      validate_codex_home_chain
      [ "$CODEX_HOME_EXISTS" -eq 1 ] || error "Codex home disappeared after creation: $CODEX_HOME"
      return 0
    fi
    # An EEXIST race is indistinguishable portably from another mkdir failure
    # without parsing errno.  Only retry when an object now exists, and validate
    # the complete chain from root before accepting it.
    if [ -e "$CODEX_HOME" ] || [ -L "$CODEX_HOME" ]; then
      continue
    fi
    error "Unable to create Codex home: $CODEX_HOME"
  done
}

# The parent holding the reserved backup leaf is the promotion boundary.  A
# different UID or a writable group/other mode can replace that leaf between
# reservation and promotion.  The effective UID is trusted here because it
# already controls CODEX_HOME; this is not a defense against same-UID peers.
validate_safe_backup_parent() {
  backup_parent_path=$1
  validate_safe_existing_object "$backup_parent_path" "backup parent"
  backup_mode=$(permission_mode "$backup_parent_path")
  case "$backup_mode" in
    [0-7][0-7][0-7]|[0-7][0-7][0-7][0-7]) ;;
    *) error "Unable to determine numeric permissions for backup parent: $backup_parent_path" ;;
  esac
  # Inspect the final group and other octal digits without shell arithmetic so
  # leading zeroes and portable /bin/sh implementations remain unambiguous.
  backup_without_other=${backup_mode%?}
  backup_group=${backup_without_other#${backup_without_other%?}}
  backup_other=${backup_mode#${backup_mode%?}}
  case "$backup_group:$backup_other" in
    [2367]:*|*:[2367]) error "Refusing group/world-writable backup parent: $backup_parent_path" ;;
  esac
}

timestamp=$(date +"%Y%m%dT%H%M%S")
BACKUP_ROOT_BASE="$CODEX_HOME/.codex-tier-routing-backups/$timestamp"
BACKUP_ROOT="$BACKUP_ROOT_BASE"
BACKUP_INDEX=1

shell_quote() {
  printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\"'\"'/g")"
}

if [ ! -d "$AGENTS_SRC" ]; then
  error "Agents directory not found: $AGENTS_SRC"
fi
if [ ! -d "$SKILL_SRC" ]; then
  error "Skill directory not found: $SKILL_SRC"
fi
if [ ! -f "$DISPATCHER_SRC" ]; then
  error "Dispatcher launcher not found: $DISPATCHER_SRC"
fi
if [ ! -f "$DISPATCHER_RUNTIME_SRC" ]; then
  error "Dispatcher Python runtime not found: $DISPATCHER_RUNTIME_SRC"
fi

# Preflight the complete source and existing destination boundaries before
# moving any active installation into the backup directory. This keeps an
# unreadable source checkout or unwritable destination from producing a
# predictable partial install.
for agent_toml in "$AGENTS_SRC"/*.toml; do
  [ -e "$agent_toml" ] || error "No agent toml files found in $AGENTS_SRC"
  [ -f "$agent_toml" ] || error "Agent source is not a file: $agent_toml"
  [ -r "$agent_toml" ] || error "Agent source is not readable: $agent_toml"
done
[ -f "$SKILL_SRC/SKILL.md" ] || error "Skill entrypoint not found: $SKILL_SRC/SKILL.md"
[ -r "$DISPATCHER_SRC" ] || error "Dispatcher launcher is not readable: $DISPATCHER_SRC"
[ -x "$DISPATCHER_SRC" ] || error "Dispatcher launcher is not executable: $DISPATCHER_SRC"
[ -r "$DISPATCHER_RUNTIME_SRC" ] || error "Dispatcher Python runtime is not readable: $DISPATCHER_RUNTIME_SRC"
/bin/sh -n "$DISPATCHER_SRC" || error "Dispatcher launcher has invalid shell syntax: $DISPATCHER_SRC"
unexpected_skill_source=$(find "$SKILL_SRC" \( -type l -o \( ! -type d ! -type f \) \) -print -quit)
[ -z "$unexpected_skill_source" ] || error "Skill source tree contains a symlink or special node: $unexpected_skill_source"
if ! find "$SKILL_SRC" -type f -exec sh -c '
  for source_file do
    [ -r "$source_file" ] || exit 1
  done
' sh {} +; then
  error "Skill source tree contains unreadable files: $SKILL_SRC"
fi
for destination_parent in "$CODEX_HOME" "$AGENTS_DST" "${SKILL_DST%/*}" "${DISPATCHER_DST%/*}" "$CODEX_HOME/.codex-tier-routing-backups"; do
  validate_codex_home_chain
  validate_safe_existing_object "$destination_parent" "install boundary"
  if [ -e "$destination_parent" ]; then
    [ ! -L "$destination_parent" ] || error "Refusing symlinked destination boundary: $destination_parent"
    [ -d "$destination_parent" ] || error "Destination boundary is not a directory: $destination_parent"
    [ -w "$destination_parent" ] || error "Destination boundary is not writable: $destination_parent"
    [ -x "$destination_parent" ] || error "Destination boundary is not traversable: $destination_parent"
  fi
done

# Validate every existing collision before moving anything. A nested object can
# be cross-owner even when its containing Codex home is owned by this account.
for agent_toml in "$AGENTS_SRC"/*.toml; do
  base=${agent_toml##*/}
  validate_safe_existing_object "$AGENTS_DST/$base" "replacement destination"
done
validate_safe_existing_object "$SKILL_DST" "replacement destination"
validate_safe_existing_object "$DISPATCHER_DST" "replacement destination"
validate_safe_existing_object "$DISPATCHER_RUNTIME_DST" "replacement destination"

STAGE_ROOT=
TRANSACTION_ROOT=
TRANSACTION_STARTED=0
TRANSACTION_COMMITTED=0
BACKUP_PARENT_CREATED=0
STRICT_CHANGED=0
STRICT_ENABLE_CLEANUP_NEEDED=0
HOME_CREATED=0
AGENTS_CREATED=0
SKILLS_CREATED=0
BIN_CREATED=0
TIER_ROUTING_CREATED=0
REPLACEMENT_COUNT=0
RESERVATION_RACE_INJECTED=0

# Test-only, no-root fault seam.  It is inert unless both variables are set,
# and intentionally rejects UID 0.  Values are replacement ordinals or
# "strict-enable".  It models process failures, not crash/power-loss safety.
maybe_inject_failure() {
  [ "$INSTALL_TEST_BUILD" -eq 1 ] || return 0
  [ "${CODEX_TIER_INSTALL_TEST_SEAM:-}" = "1" ] || return 0
  [ "$(id -u)" != "0" ] || return 0
  case "${CODEX_TIER_INSTALL_TEST_FAIL_AFTER:-}" in
    "$1") error "Injected installer failure after $1" ;;
    "signal-$1")
      kill -TERM "$$"
      error "Injected installer signal after $1 was not delivered"
      ;;
  esac
}

maybe_inject_reservation_race() {
  [ "$INSTALL_TEST_BUILD" -eq 1 ] || return 0
  [ "${CODEX_TIER_INSTALL_TEST_SEAM:-}" = "1" ] || return 0
  [ "${CODEX_TIER_INSTALL_TEST_FAIL_AFTER:-}" = "reservation-race" ] || return 0
  [ "$(id -u)" != "0" ] || return 0
  [ "$RESERVATION_RACE_INJECTED" -eq 0 ] || return 0
  # Test-only: model another process winning the final-leaf mkdir race.
  mkdir -m 700 "$BACKUP_ROOT" || error "Unable to inject backup reservation race"
  RESERVATION_RACE_INJECTED=1
}

maybe_inject_home_creation_race() {
  [ "$INSTALL_TEST_BUILD" -eq 1 ] || return 0
  [ "${CODEX_TIER_INSTALL_TEST_SEAM:-}" = "1" ] || return 0
  [ "${CODEX_TIER_INSTALL_TEST_FAIL_AFTER:-}" = "home-create-race" ] || return 0
  [ "$(id -u)" != "0" ] || return 0
  # Model an object winning mkdir; the next loop iteration validates it from /.
  mkdir -m 777 "$CODEX_HOME" 2>/dev/null || :
}

maybe_inject_prepromotion_race() {
  [ "$INSTALL_TEST_BUILD" -eq 1 ] || return 0
  [ "${CODEX_TIER_INSTALL_TEST_SEAM:-}" = "1" ] || return 0
  [ "${CODEX_TIER_INSTALL_TEST_FAIL_AFTER:-}" = "prepromotion-race" ] || return 0
  [ "$(id -u)" != "0" ] || return 0
  chmod 777 "$CODEX_HOME" 2>/dev/null || :
}

remove_created_empty_parents() {
  [ "$TIER_ROUTING_CREATED" -eq 0 ] || rmdir "$CODEX_HOME/tier-routing" 2>/dev/null || :
  [ "$BIN_CREATED" -eq 0 ] || rmdir "$CODEX_HOME/bin" 2>/dev/null || :
  [ "$SKILLS_CREATED" -eq 0 ] || rmdir "$CODEX_HOME/skills" 2>/dev/null || :
  [ "$AGENTS_CREATED" -eq 0 ] || rmdir "$CODEX_HOME/agents" 2>/dev/null || :
  [ "$HOME_CREATED" -eq 0 ] || rmdir "$CODEX_HOME" 2>/dev/null || :
}

restore_target() {
  restore_path=$1
  restore_rel=$2
  restore_backup="$TRANSACTION_ROOT/$restore_rel"
  restore_state="$TRANSACTION_ROOT/.state/$restore_rel"
  [ -e "$restore_state" ] || return 0
  restore_original=$(cat "$restore_state" 2>/dev/null || printf invalid)
  case "$restore_original" in 0|1) ;; *) printf 'Unable to read transaction state: %s\n' "$restore_state" >&2; return 1 ;; esac
  if [ -e "$restore_backup" ] || [ -L "$restore_backup" ]; then
    if ! rm -rf "$restore_path"; then
      printf 'Unable to remove failed replacement during rollback: %s\n' "$restore_path" >&2
      return 1
    fi
    if ! mkdir -p "${restore_path%/*}" || ! mv "$restore_backup" "$restore_path"; then
      printf 'Unable to restore transaction backup: %s\n' "$restore_backup" >&2
      return 1
    fi
  elif [ "$restore_original" -eq 0 ]; then
    # No original existed: remove only a partially installed replacement.
    if ! rm -rf "$restore_path"; then
      printf 'Unable to remove partial replacement during rollback: %s\n' "$restore_path" >&2
      return 1
    fi
  else
    # The move never materialized.  The original destination is still live.
    :
  fi
}

rollback_transaction() {
  [ "$TRANSACTION_STARTED" -eq 1 ] || return 0
  # If a signal arrived in the narrow interval after the commit rename but
  # before the shell updated TRANSACTION_ROOT, follow the retained markers.
  if [ ! -e "$TRANSACTION_ROOT/.state" ] && [ -e "$BACKUP_ROOT/snapshot/.state" ]; then
    TRANSACTION_ROOT=$BACKUP_ROOT/snapshot
  fi
  rollback_status=0
  if [ "$STRICT_ENABLE_CLEANUP_NEEDED" -eq 1 ] || [ "$STRICT_CHANGED" -eq 1 ]; then
    if ! run_dispatcher strict-disable; then
      printf 'Unable to restore strict configuration during bundle rollback\n' >&2
      return 1
    fi
    STRICT_ENABLE_CLEANUP_NEEDED=0
    STRICT_CHANGED=0
  fi
  restore_target "$DISPATCHER_DST" "bin/codex-tier" || rollback_status=1
  restore_target "$DISPATCHER_RUNTIME_DST" "bin/codex-tier.py" || rollback_status=1
  restore_target "$SKILL_DST" "skills/model-routing" || rollback_status=1
  for restore_source in "$AGENTS_SRC"/*.toml; do
    restore_base=${restore_source##*/}
    restore_target "$AGENTS_DST/$restore_base" "agents/$restore_base" || rollback_status=1
  done
  if [ "$rollback_status" -eq 0 ]; then
    rollback_root=$TRANSACTION_ROOT
    rm -rf "$TRANSACTION_ROOT" || rollback_status=1
    case "$rollback_root" in */snapshot) rmdir "${rollback_root%/*}" 2>/dev/null || : ;; esac
  fi
  if [ "$rollback_status" -eq 0 ]; then
    TRANSACTION_ROOT=
    TRANSACTION_STARTED=0
    [ "$BACKUP_PARENT_CREATED" -eq 0 ] || rmdir "${BACKUP_ROOT%/*}" 2>/dev/null || :
    remove_created_empty_parents
  else
    printf 'Installer rollback incomplete; preserved recovery state at: %s\n' "$TRANSACTION_ROOT" >&2
  fi
  return "$rollback_status"
}

finish() {
  status=$?
  trap - 0 1 2 15
  set +e
  if [ "$status" -ne 0 ] && [ "$TRANSACTION_COMMITTED" -eq 0 ]; then
    rollback_transaction || :
  fi
  [ -z "$STAGE_ROOT" ] || rm -rf "$STAGE_ROOT"
  exit "$status"
}
trap finish 0
trap 'exit 129' 1
trap 'exit 130' 2
trap 'exit 143' 15

if [ "$STRICT" -eq 1 ]; then
  validate_runtime_path
fi

if [ "$DRY_RUN" -eq 1 ]; then
  for source_path in "$AGENTS_SRC"/*.toml; do
    base=${source_path##*/}
    echo "Would install: $source_path -> $AGENTS_DST/$base"
  done
  echo "Would install: $SKILL_SRC -> $SKILL_DST"
  echo "Would install: $DISPATCHER_SRC -> $DISPATCHER_DST"
  echo "Would install: $DISPATCHER_RUNTIME_SRC -> $DISPATCHER_RUNTIME_DST"
  [ "$STRICT" -eq 0 ] || echo "Would enable strict routing in: $CODEX_HOME/config.toml"
  echo "Dry-run complete. No files were changed."
  exit 0
fi

ensure_codex_home
STAGE_ROOT="$CODEX_HOME/.codex-tier-routing-stage.$$"
mkdir -m 700 "$STAGE_ROOT"
mkdir -p "$STAGE_ROOT/agents" "$STAGE_ROOT/skills" "$STAGE_ROOT/bin"
for source_path in "$AGENTS_SRC"/*.toml; do
  cp "$source_path" "$STAGE_ROOT/agents/" || error "Unable to stage agent source: $source_path"
done
cp -R "$SKILL_SRC" "$STAGE_ROOT/skills/model-routing" || error "Unable to stage skill source: $SKILL_SRC"
cp "$DISPATCHER_SRC" "$STAGE_ROOT/bin/codex-tier" || error "Unable to stage dispatcher source: $DISPATCHER_SRC"
cp "$DISPATCHER_RUNTIME_SRC" "$STAGE_ROOT/bin/codex-tier.py" || error "Unable to stage dispatcher Python runtime: $DISPATCHER_RUNTIME_SRC"
chmod 700 "$STAGE_ROOT/agents" "$STAGE_ROOT/skills" "$STAGE_ROOT/bin"
chmod 600 "$STAGE_ROOT/agents/"*.toml
chmod 700 "$STAGE_ROOT/bin/codex-tier"
chmod 600 "$STAGE_ROOT/bin/codex-tier.py"
normalize_private_tree "$STAGE_ROOT/skills/model-routing"
validate_private_tree "$STAGE_ROOT/skills/model-routing" "staged skill tree"
for source_path in "$AGENTS_SRC"/*.toml; do
  base=${source_path##*/}
  cmp -s "$source_path" "$STAGE_ROOT/agents/$base" || error "Staged agent verification failed: $source_path"
done
diff -qr "$SKILL_SRC" "$STAGE_ROOT/skills/model-routing" >/dev/null || error "Staged skill verification failed: $SKILL_SRC"
cmp -s "$DISPATCHER_SRC" "$STAGE_ROOT/bin/codex-tier" || error "Staged dispatcher verification failed: $DISPATCHER_SRC"
cmp -s "$DISPATCHER_RUNTIME_SRC" "$STAGE_ROOT/bin/codex-tier.py" || error "Staged dispatcher Python verification failed: $DISPATCHER_RUNTIME_SRC"

TRANSACTION_ROOT="$CODEX_HOME/.codex-tier-routing-transaction.$$"
mkdir -m 700 "$TRANSACTION_ROOT"
TRANSACTION_STARTED=1
[ -e "$AGENTS_DST" ] || { mkdir -m 700 "$AGENTS_DST"; AGENTS_CREATED=1; }
[ -e "${SKILL_DST%/*}" ] || { mkdir -m 700 "${SKILL_DST%/*}"; SKILLS_CREATED=1; }
[ -e "${DISPATCHER_DST%/*}" ] || { mkdir -m 700 "${DISPATCHER_DST%/*}"; BIN_CREATED=1; }

replace_file() {
  source_path=$1
  dest_path=$2
  rel=$3
  replacement_ordinal=$((REPLACEMENT_COUNT + 1))
  mkdir -p "$TRANSACTION_ROOT/.state/${rel%/*}"
  if [ -e "$dest_path" ] || [ -L "$dest_path" ]; then
    printf '1\n' > "$TRANSACTION_ROOT/.state/$rel"
    maybe_inject_failure "pre-move-$replacement_ordinal"
    maybe_inject_failure "move-failure-$replacement_ordinal"
    mkdir -p "$TRANSACTION_ROOT/${rel%/*}"
    mv "$dest_path" "$TRANSACTION_ROOT/$rel" || error "Unable to move replacement destination: $dest_path"
  fi
  [ -e "$TRANSACTION_ROOT/.state/$rel" ] || { printf '0\n' > "$TRANSACTION_ROOT/.state/$rel"; maybe_inject_failure "pre-move-$replacement_ordinal"; }
  maybe_inject_failure "post-move-$replacement_ordinal"
  cp -p "$source_path" "$dest_path" || error "Unable to install file: $dest_path"
  cmp -s "$source_path" "$dest_path" || error "Installed file verification failed: $dest_path"
  REPLACEMENT_COUNT=$((REPLACEMENT_COUNT + 1))
  maybe_inject_failure "$REPLACEMENT_COUNT"
  echo "Installed: $dest_path"
}

replace_dir() {
  source_path=$1
  dest_path=$2
  rel=$3
  replacement_ordinal=$((REPLACEMENT_COUNT + 1))
  mkdir -p "$TRANSACTION_ROOT/.state/${rel%/*}"
  if [ -e "$dest_path" ] || [ -L "$dest_path" ]; then
    printf '1\n' > "$TRANSACTION_ROOT/.state/$rel"
    maybe_inject_failure "pre-move-$replacement_ordinal"
    maybe_inject_failure "move-failure-$replacement_ordinal"
    mkdir -p "$TRANSACTION_ROOT/${rel%/*}"
    mv "$dest_path" "$TRANSACTION_ROOT/$rel" || error "Unable to move replacement destination: $dest_path"
  fi
  [ -e "$TRANSACTION_ROOT/.state/$rel" ] || { printf '0\n' > "$TRANSACTION_ROOT/.state/$rel"; maybe_inject_failure "pre-move-$replacement_ordinal"; }
  maybe_inject_failure "post-move-$replacement_ordinal"
  cp -R -p "$source_path" "$dest_path" || error "Unable to install directory: $dest_path"
  diff -qr "$source_path" "$dest_path" >/dev/null || error "Installed directory verification failed: $dest_path"
  REPLACEMENT_COUNT=$((REPLACEMENT_COUNT + 1))
  maybe_inject_failure "$REPLACEMENT_COUNT"
  echo "Installed: $dest_path"
}

INSTALL_FILES=""
for source_path in "$AGENTS_SRC"/*.toml; do
  base=${source_path##*/}
  dest_path="$AGENTS_DST/$base"
  replace_file "$STAGE_ROOT/agents/$base" "$dest_path" "agents/$base"
  INSTALL_FILES="$INSTALL_FILES $(shell_quote "$dest_path")"
done
replace_dir "$STAGE_ROOT/skills/model-routing" "$SKILL_DST" "skills/model-routing"
replace_file "$STAGE_ROOT/bin/codex-tier" "$DISPATCHER_DST" "bin/codex-tier"
replace_file "$STAGE_ROOT/bin/codex-tier.py" "$DISPATCHER_RUNTIME_DST" "bin/codex-tier.py"
for installed_agent in "$AGENTS_DST"/*.toml; do
  validate_private_object "$installed_agent" 600 "installed agent profile"
done
validate_private_tree "$SKILL_DST" "installed skill tree"
validate_private_object "$DISPATCHER_DST" 700 "installed dispatcher"
validate_private_object "$DISPATCHER_RUNTIME_DST" 600 "installed dispatcher Python runtime"
if [ "$STRICT" -eq 1 ]; then
  [ -e "$CODEX_HOME/tier-routing" ] || TIER_ROUTING_CREATED=1
  if [ ! -e "$CODEX_HOME/tier-routing/strict-config.json" ] && [ ! -L "$CODEX_HOME/tier-routing/strict-config.json" ]; then
    STRICT_ENABLE_CLEANUP_NEEDED=1
  fi
  maybe_inject_failure strict-enable
  if ! STRICT_OUTPUT=$(run_dispatcher strict-enable); then
    printf '%s\n' "$STRICT_OUTPUT"
    error "Strict routing could not be enabled; bundle transaction restored"
  fi
  maybe_inject_failure strict-enable-succeeded
  printf '%s\n' "$STRICT_OUTPUT"
  case "$STRICT_OUTPUT" in
    *'"strict_mode": "enabled"'*) STRICT_CHANGED=1 ;;
  esac
fi

# Commit only after every replacement and requested strict enablement passed.
# Do not leave a timestamped collision-backup directory when there was no
# collision to preserve.
if [ -e "$TRANSACTION_ROOT/agents" ] || [ -e "$TRANSACTION_ROOT/skills" ] || [ -e "$TRANSACTION_ROOT/bin" ]; then
  backup_parent=${BACKUP_ROOT%/*}
  if [ ! -e "$backup_parent" ] && [ ! -L "$backup_parent" ]; then
    mkdir -m 700 "$backup_parent"
    BACKUP_PARENT_CREATED=1
  fi
  [ ! -L "$backup_parent" ] && [ -d "$backup_parent" ] || error "Backup parent is not a safe directory: $backup_parent"
  validate_safe_backup_parent "$backup_parent"
  # No preflight result is trusted across staging/replacement work.
  maybe_inject_prepromotion_race
  validate_codex_home_chain
  validate_safe_backup_parent "$backup_parent"
  transaction_device=$(device_id "$TRANSACTION_ROOT")
  backup_device=$(device_id "$backup_parent")
  case "$transaction_device" in ''|*[!0-9]*) error "Unable to determine numeric transaction device identity for backup promotion" ;; esac
  case "$backup_device" in ''|*[!0-9]*) error "Unable to determine numeric backup device identity for backup promotion" ;; esac
  [ "$transaction_device" = "$backup_device" ] || error "Transaction root and backup parent are on different devices"
  backup_reserved=0
  while [ "$BACKUP_INDEX" -le 100 ]; do
    if [ -e "$BACKUP_ROOT" ] || [ -L "$BACKUP_ROOT" ]; then
      BACKUP_ROOT="${BACKUP_ROOT_BASE}_$BACKUP_INDEX"; BACKUP_INDEX=$((BACKUP_INDEX + 1)); continue
    fi
    maybe_inject_reservation_race
    if mkdir -m 700 "$BACKUP_ROOT" 2>/dev/null; then backup_reserved=1; break; fi
    if [ -e "$BACKUP_ROOT" ] || [ -L "$BACKUP_ROOT" ]; then
      BACKUP_ROOT="${BACKUP_ROOT_BASE}_$BACKUP_INDEX"; BACKUP_INDEX=$((BACKUP_INDEX + 1)); continue
    fi
    error "Unable to reserve backup directory: $BACKUP_ROOT"
  done
  [ "$backup_reserved" -eq 1 ] || error "Unable to reserve a bounded backup directory"
  # The reserved leaf prevents races; the single renamed tree lives below it.
  mv "$TRANSACTION_ROOT" "$BACKUP_ROOT/snapshot"
  TRANSACTION_ROOT=$BACKUP_ROOT/snapshot
  maybe_inject_failure backup-promotion
  TRANSACTION_COMMITTED=1
  rm -rf "$TRANSACTION_ROOT/.state" || printf 'Warning: unable to remove transaction markers from backup\n' >&2
  TRANSACTION_ROOT=
else
  TRANSACTION_COMMITTED=1
  rm -rf "$TRANSACTION_ROOT" || printf 'Warning: unable to remove empty transaction state\n' >&2
  TRANSACTION_ROOT=
fi
rm -rf "$STAGE_ROOT"
STAGE_ROOT=

# Disable only an unchanged strict configuration managed by this installation.
# A drifted config halts before removing the helper so the user can reconcile it.
if [ "$STRICT" -eq 1 ]; then
  uninstall_codex_bin=
  if [ "${CODEX_TIER_CODEX_BIN+x}" = x ]; then
    uninstall_codex_bin=" $(shell_quote "CODEX_TIER_CODEX_BIN=$CODEX_TIER_CODEX_BIN")"
  fi
  uninstall_cmd="$(shell_quote "$ENV_BIN") -i $(shell_quote "HOME=$HOME") $(shell_quote "PATH=$ORIGINAL_PATH") $(shell_quote "CODEX_HOME=$CODEX_HOME")$uninstall_codex_bin $(shell_quote "$PYTHON_BIN") -I $(shell_quote "$DISPATCHER_RUNTIME_DST") strict-disable && $(shell_quote "$RM_BIN") -f$INSTALL_FILES $(shell_quote "$DISPATCHER_DST") $(shell_quote "$DISPATCHER_RUNTIME_DST") && $(shell_quote "$RM_BIN") -rf $(shell_quote "$SKILL_DST")"
else
  uninstall_cmd="$(shell_quote "$RM_BIN") -f$INSTALL_FILES $(shell_quote "$DISPATCHER_DST") $(shell_quote "$DISPATCHER_RUNTIME_DST") && $(shell_quote "$RM_BIN") -rf $(shell_quote "$SKILL_DST")"
fi
printf 'Exact uninstall command for installed files:\n%s\n' "$uninstall_cmd"
