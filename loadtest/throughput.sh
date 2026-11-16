#!/usr/bin/env bash
# Throughput at 1, 2 and 3 workers (ARCHITECTURE.md §13). Run as root in a distro set up by
# deploy/bootstrap.sh, with k6 installed:
#     bash loadtest/throughput.sh > throughput.md
# Settings (environment): VUS=30 contestants, DURATION_S=180 per run, WARMUP_S=30 left out of the
# numbers, WORKER_COUNTS="1 2 3", LANGUAGES=cpp,python.
#
# For each worker count it waits for the queue to empty, starts exactly that many judge-worker
# units, lets k6 submit for DURATION_S, and reads the results from the database. The workers that
# were running before are put back at the end, whatever happens.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
VUS=${VUS:-30} DURATION_S=${DURATION_S:-180} WARMUP_S=${WARMUP_S:-30}
WORKER_COUNTS=${WORKER_COUNTS:-"1 2 3"} LANGUAGES=${LANGUAGES:-cpp,python}
K6=${K6:-k6}

set -a; . /etc/judge/judge.env; set +a
work=$(mktemp -d)
db() { docker exec -i judge-postgres psql -U judge -d judge -qtA -F ' | ' -c "$1"; }
OURS="owner_type = 'user' AND owner_id IN (SELECT id FROM users WHERE email LIKE 'loadtest-%@loadtest.invalid')"

before=$(systemctl list-units 'judge-worker@*' --state=active --plain --no-legend | awk '{print $1}')
restore() {
  systemctl stop 'judge-worker@*'
  [ -z "$before" ] || systemctl start $before
  rm -rf "$work"
}
trap restore EXIT

wait_for_empty_queue() {
  for _ in $(seq 600); do
    [ "$(db "SELECT count(*) FROM jobs WHERE status IN ('queued', 'running')")" = 0 ] && return
    sleep 1
  done
  echo "the queue didn't empty in 10 minutes" >&2; exit 1
}

cd "$ROOT"
/opt/judge-venv/bin/python -m loadtest.prepare --users "$VUS" --out "$work/setup.json" >&2

echo "# Throughput"
echo
echo "Measured $(date -u '+%Y-%m-%d %H:%M') UTC on $(hostname): $(grep -m1 'model name' /proc/cpuinfo | cut -d: -f2 | xargs)," \
     "$(nproc) CPUs, $($K6 version | cut -d' ' -f1-2). $VUS contestants submit with no pause between submissions" \
     "(a correct answer, languages $LANGUAGES in turn), so the queue is never empty and the workers set the pace." \
     "Each run lasts ${DURATION_S} s; the first ${WARMUP_S} s are left out. k6 and the API run on the same machine as the workers." \
     "Times come from the database: verdict time is submission to result, queue wait is submission to a worker claiming it. Times in ms."
echo
echo "| Workers | Judged | Jobs/s | Verdict p50 | p95 | p99 | Queue wait p50 | p95 | Not AC | 429s |"
echo "|---|---|---|---|---|---|---|---|---|---|"
per_language=""
for workers in $WORKER_COUNTS; do
  wait_for_empty_queue
  systemctl stop 'judge-worker@*'
  systemctl start $(seq -f 'judge-worker@%g' 1 "$workers")
  sleep 2
  t0=$(db "SELECT now()")
  status=0
  "$K6" run --quiet --no-color -e BASE_URL="http://127.0.0.1:${API_PORT:-8000}" -e SETUP="$work/setup.json" \
    -e VUS="$VUS" -e DURATION="${DURATION_S}s" -e LANGUAGES="$LANGUAGES" \
    loadtest/throughput.js > "$work/k6-$workers.txt" 2>&1 || status=$?
  window="j.submitted_at >= '$t0'::timestamptz + interval '$WARMUP_S seconds'
          AND j.submitted_at < '$t0'::timestamptz + interval '$DURATION_S seconds'"
  finished="j.finished_at >= '$t0'::timestamptz + interval '$WARMUP_S seconds'
            AND j.finished_at < '$t0'::timestamptz + interval '$DURATION_S seconds'"
  rate=$(db "SELECT count(*), round(count(*) / ($DURATION_S - $WARMUP_S)::numeric, 2) FROM jobs j WHERE $OURS AND $finished")
  times=$(db "SELECT round(1000 * percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM j.finished_at - j.submitted_at))),
                     round(1000 * percentile_cont(0.95) WITHIN GROUP (ORDER BY extract(epoch FROM j.finished_at - j.submitted_at))),
                     round(1000 * percentile_cont(0.99) WITHIN GROUP (ORDER BY extract(epoch FROM j.finished_at - j.submitted_at))),
                     round(1000 * percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM j.started_at - j.submitted_at))),
                     round(1000 * percentile_cont(0.95) WITHIN GROUP (ORDER BY extract(epoch FROM j.started_at - j.submitted_at)))
              FROM jobs j WHERE $OURS AND $window AND j.status = 'completed'")
  wrong=$(db "SELECT count(*) FROM jobs j LEFT JOIN results r ON r.job_id = j.id
              WHERE $OURS AND j.submitted_at >= '$t0' AND (j.status <> 'completed' OR r.verdict IS DISTINCT FROM 'AC')
              AND j.status NOT IN ('queued', 'running')")
  rejected=$(grep -o 'rejected_429[^0-9]*[0-9]*' "$work/k6-$workers.txt" | grep -o '[0-9]*$' || echo 0)
  echo "| $workers | $rate | $times | $wrong | ${rejected:-0} |"
  per_language+=$(db "SELECT $workers, j.language, count(*),
                             round(1000 * percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM j.finished_at - j.started_at)))
                      FROM jobs j WHERE $OURS AND $window AND j.status = 'completed' GROUP BY j.language ORDER BY j.language")$'\n'
  if [ "$status" != 0 ]; then
    echo "k6 exited with $status at $workers workers:" >&2; tail -40 "$work/k6-$workers.txt" >&2
  fi
done

echo
echo "**Judging time per job** (a worker claiming it to its result, median, ms), by language:"
echo
echo "| Workers | Language | Jobs | Judging p50 |"
echo "|---|---|---|---|"
printf '%s' "$per_language" | sed '/^$/d; s/^/| /; s/$/ |/'
