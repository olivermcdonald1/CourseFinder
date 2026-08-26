#!/bin/bash
#
# Who is using the site, from the captured request log.
#
# Prefers logs/launch-*.log (a running `fly logs` capture, which keeps history)
# and falls back to whatever Fly still holds, which is roughly the last hour.
# Start a capture with:  fly logs -a APP > logs/launch-$(date +%F-%H%M).log &
#
# Counts distinct client IPs. Approximate in both directions, and worth knowing
# which way: everyone behind McGill's campus NAT counts as one person, and one
# person on wifi then cellular counts as two.
APP="${1:-mcgill-course-finder}"

cap=$(ls -t logs/launch-*.log 2>/dev/null | head -1)
if [ -n "$cap" ] && [ -s "$cap" ]; then
  log=$(grep -v '172\.19\.' "$cap")
  src="$cap"
else
  log=$(fly logs -a "$APP" --no-tail 2>/dev/null | grep -v '172\.19\.')
  src="fly logs (last ~1h only)"
fi

hits=$(printf '%s\n' "$log" | grep -oE '[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+:[0-9]+ - "GET [^ ]*')

printf 'source: %s\n\n' "$src"

ips=$(printf '%s\n' "$hits" | cut -d: -f1 | sort -u | grep .)
printf 'VISITORS: %s\n\n' "$(printf '%s\n' "$ips" | grep -c .)"

printf '  %-17s %7s %7s %8s   %s\n' IP REQUESTS VISITS COURSES WHERE
printf '  %-17s %7s %7s %8s   %s\n' ----------------- ------- ------ ------- -----
printf '%s\n' "$ips" | while read -r ip; do
  mine=$(printf '%s\n' "$hits" | grep "^$ip:")
  n=$(printf '%s\n'  "$mine" | grep -c .)
  # A boot fetches /courses/filters exactly once, so it counts real page loads
  # without also counting every filter click as a fresh visit.
  v=$(printf '%s\n'  "$mine" | grep -c '/courses/filters')
  # Detail requests include prefetches, so this is interest, not clicks.
  c=$(printf '%s\n'  "$mine" | grep -cE 'GET /courses/[A-Z]')
  case "$ip" in
    132.216.*) where="McGill campus" ;;
    *)         where="off campus" ;;
  esac
  printf '  %-17s %7s %7s %8s   %s\n' "$ip" "$n" "$v" "$c" "$where"
done

printf '\nWHAT THEY SEARCHED FOR\n'
printf '%s\n' "$hits" | grep -oE 'keywords=[^&]*' | sed 's/keywords=//' \
  | sort | uniq -c | sort -rn | head -8 | sed 's/^/  /'
printf '%s\n' "$hits" | grep -oE 'course_id=[^&]*' | sed 's/course_id=//' \
  | sort | uniq -c | sort -rn | head -5 | sed 's/^/  /'

printf '\nCOURSES OPENED (most looked at)\n'
printf '%s\n' "$hits" | grep -oE 'GET /courses/[A-Z][A-Z0-9-]+' \
  | sed 's|GET /courses/||' | sort | uniq -c | sort -rn | head -8 | sed 's/^/  /'

printf '\nPROBLEMS\n'
printf '  429 rate limited : %s\n' "$(printf '%s\n' "$log" | grep -c ' 429 ')"
printf '  5xx server errors: %s\n' "$(printf '%s\n' "$log" | grep -cE ' 50[0-9] ')"
