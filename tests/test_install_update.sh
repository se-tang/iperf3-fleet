#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

git init --bare -q -b main "$TMP/remote.git"
git init -q -b main "$TMP/seed"
git -C "$TMP/seed" -c user.name=test -c user.email=test@example.com \
  -c commit.gpgsign=false commit --allow-empty -q -m init
printf 'remote-v1\n' >"$TMP/seed/app.txt"
git -C "$TMP/seed" add app.txt
git -C "$TMP/seed" -c user.name=test -c user.email=test@example.com \
  -c commit.gpgsign=false commit -q -m v1
git -C "$TMP/seed" remote add origin "$TMP/remote.git"
git -C "$TMP/seed" push -q -u origin main

git clone -q "$TMP/remote.git" "$TMP/work"
printf 'local-change\n' >>"$TMP/work/app.txt"
printf 'untracked-change\n' >"$TMP/work/local-only.txt"

printf 'remote-v2\n' >"$TMP/seed/app.txt"
git -C "$TMP/seed" add app.txt
git -C "$TMP/seed" -c user.name=test -c user.email=test@example.com \
  -c commit.gpgsign=false commit -q -m v2
git -C "$TMP/seed" push -q

sed -n '/^# --- update helpers/,/^# --- end update helpers/p' "$ROOT/install.sh" \
  >"$TMP/helpers.sh"

INSTALL_DIR="$TMP/work"
BRANCH=main
LOG_FILE="$TMP/update.log"
warn() { printf '%s\n' "$*" >&2; }
log_file() { printf '%s\n' "$*" >>"$LOG_FILE"; }
# shellcheck disable=SC1091
. "$TMP/helpers.sh"

git -C "$INSTALL_DIR" fetch -q origin "$BRANCH"
align_repo_to_remote

test "$(git -C "$INSTALL_DIR" rev-parse HEAD)" = \
     "$(git -C "$INSTALL_DIR" rev-parse "origin/$BRANCH")"
test "$(cat "$INSTALL_DIR/app.txt")" = "remote-v2"
test -z "$(git -C "$INSTALL_DIR" status --porcelain)"
test -n "$LOCAL_BACKUP"
test "$(git -C "$INSTALL_DIR" stash list | wc -l)" -eq 1
git -C "$INSTALL_DIR" show "$LOCAL_BACKUP:app.txt" | grep -q 'local-change'
git -C "$INSTALL_DIR" show "$LOCAL_BACKUP^3:local-only.txt" | grep -q 'untracked-change'

printf 'install.sh dirty-worktree update OK\n'
