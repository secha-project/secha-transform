#!/usr/bin/env bash
# Stage Kempower canonical parquet on the cluster NFS one landed part at a time, MERGE each
# part into secha.canonical.measurement, then MERGE the same parts' sessions into
# secha.canonical.charging_session. Run from the secha-transform root, VPN on.
#
#   bash scripts/phase3/load_kempower.sh PART...       # e.g. 10 20 30: parts 10, 20 and 30
#
# One part (about 3.7M rows) per MERGE. Delta first copies a MERGE source that is not a Delta
# table to executor disk, and on this platform that disk is a 32 GB swap-backed /tmp
# (SPARK_LOCAL_DIRS) shared with the executors' memory and other users' jobs. A 10-part MERGE
# (about 37M rows) filled it; one part copies about 1.7 GB. Every part is checked against the
# local files before anything is staged, each staged file is checked on the NFS before its
# MERGE, and a failed MERGE leaves the table unchanged (Delta commits atomically), so the
# script stops at the first failure and any part can simply be re-run. After a failure, read
# the reason from the Spark UI's REST API (docs/phase3-log.md, 2026-09-24) rather than
# running the MERGE again.
#
# The paths pin the 2026-09-24 load (export 95d29330, staging directory load-003); a new
# export gets a new load-NNN directory.
set -euo pipefail
shopt -s nullglob
export MSYS_NO_PATHCONV=1  # Git Bash would otherwise rewrite /net/nfs/... into a Windows path

HOST=sparky@130.230.115.138
REMOTE=/net/nfs/data/secha/canonical-staging/load-003
EXPORT=95d29330
MEASUREMENTS="data/canonical/source_vendor=kempower/event_date=__HIVE_DEFAULT_PARTITION__"
SESSIONS="data/canonical-dimensions/charging_session/source_vendor=kempower"
CLI=.venv/Scripts/secha-transform.exe             # Windows venv layout
[[ -x "$CLI" ]] || CLI=.venv/bin/secha-transform  # POSIX venv layout

if (( $# == 0 )); then
  echo "usage: $0 PART..." >&2
  exit 2
fi

ids=()
for part in "$@"; do
  if [[ ! "$part" =~ ^[0-9]+$ ]]; then
    echo "not a part number: $part" >&2
    exit 2
  fi
  id=$(printf "%05d" "$((10#$part))")  # base 10: 08 is part 8, not a bad octal number
  found=( "$MEASUREMENTS/export-$EXPORT-part-$id-c000-b"*.parquet )
  if (( ${#found[@]} == 0 )); then
    echo "part $part: no canonical files in $MEASUREMENTS" >&2
    exit 1
  fi
  ids+=( "$id" )
done

stage() {  # stage DIR FILE...: copy the files into DIR on the NFS; stop unless all landed
  local dir=$1 landed
  shift
  ssh -o BatchMode=yes "$HOST" "mkdir -p '$dir'"
  scp -o BatchMode=yes -q "$@" "$HOST:$dir/"
  landed=$(ssh -o BatchMode=yes "$HOST" "cd '$dir' && ls ${*##*/} 2>/dev/null | wc -l")
  if (( landed != $# )); then
    echo "$dir: $landed of $# files landed; stopping" >&2
    exit 1
  fi
}

for id in "${ids[@]}"; do
  files=( "$MEASUREMENTS/export-$EXPORT-part-$id-c000-b"*.parquet )
  started=$(date +%s)
  stage "$REMOTE/part-$id/source_vendor=kempower/event_date=__HIVE_DEFAULT_PARTITION__" "${files[@]}"
  staged=$(( $(date +%s) - started ))
  "$CLI" delta-load --staging "$REMOTE/part-$id" | grep "MERGE into"
  echo "part $((10#$id)) (${#files[@]} files): staged in ${staged}s, loaded in" \
       "$(( $(date +%s) - started - staged ))s"
done

# The same parts' sessions in one MERGE: a few MB, keyed on session_id, so sessions staged by
# an earlier run merge again unchanged.
sessions=()
for id in "${ids[@]}"; do
  sessions+=( "$SESSIONS/export-$EXPORT-part-$id-c000-b"*.parquet )
done
if (( ${#sessions[@]} == 0 )); then
  echo "no session files for these parts in $SESSIONS" >&2
  exit 1
fi
stage "$REMOTE/charging_session/source_vendor=kempower" "${sessions[@]}"
"$CLI" delta-load --staging "$REMOTE/charging_session" --entity charging_session | grep "MERGE into"
