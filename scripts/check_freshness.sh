#!/bin/bash
#
# Seat age on the deployed site, both terms.
#
# The footer claims a daily sweep, so anything past ~36h means the collector is
# failing and nothing said so. That is exactly how it failed for three days:
# the job ran, exited 1 in 42 seconds, and the only symptom was numbers quietly
# getting older behind a line of copy promising they were fresh.
BASE="${1:-https://mcgill-course-finder.fly.dev}"
rc=0

for t in 202609 202701; do
  body=$(curl -s --compressed --max-time 20 \
         -w '\n%{http_code}' "$BASE/courses?limit=1&term=$t&undergrad_only=true")
  code="${body##*$'\n'}"
  json="${body%$'\n'*}"

  if [ "$code" != "200" ]; then
    # A monitoring script that dies on a stack trace tells you less than one
    # that names the status it got. 429 and 5xx mean different things.
    printf '  %s  HTTP %s -- no reading\n' "$t" "$code"
    rc=1
    continue
  fi

  printf '%s' "$json" | python3 -c "
import json, sys, datetime
try:
    rows = json.load(sys.stdin)['courses']
except Exception as exc:
    print(f'  $t  unreadable response ({exc.__class__.__name__})'); raise SystemExit(1)
if not rows:
    print('  $t  no courses returned'); raise SystemExit(1)
stamp = rows[0]['observed_at']
if stamp is None:
    print('  $t  never swept'); raise SystemExit(1)
dt = datetime.datetime.fromisoformat(stamp.replace('Z', '+00:00'))
hours = int((datetime.datetime.now(datetime.timezone.utc) - dt).total_seconds() // 3600)
print(f'  $t  {stamp}  ({hours}h old)' + ('   <-- STALE' if hours > 36 else ''))
raise SystemExit(1 if hours > 36 else 0)
" || rc=1
done

exit $rc
