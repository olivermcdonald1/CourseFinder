#!/bin/bash
#
# Roughly how many people used the site, from the request log.
#
# Counts distinct client IPs that loaded the page. Approximate in both
# directions and worth knowing which: everyone behind one campus NAT counts
# once, and one person on wifi then cellular counts twice. Good enough to tell
# three visitors from thirty; not a number to quote.
#
# The log is whatever Fly still holds -- roughly the last hour, not history.
# For anything durable this needs to be written down as it happens.
APP="${1:-mcgill-course-finder}"

log=$(fly logs -a "$APP" --no-tail 2>/dev/null \
      | grep -v '172\.19\.' )        # internal health checks, not people

ips=$(printf '%s\n' "$log" | grep -oE '[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+:[0-9]+ - "GET /' \
      | cut -d: -f1 | sort -u)

n=$(printf '%s\n' "$ips" | grep -c . )
printf 'distinct visitors in the retained log: %s\n\n' "$n"

printf '%s\n' "$ips" | grep . | while read -r ip; do
  hits=$(printf '%s\n' "$log" | grep -c "$ip")
  boots=$(printf '%s\n' "$log" | grep "$ip" | grep -c 'GET /courses/filters')
  printf '  %-16s %4s requests   %2s page loads\n' "$ip" "$hits" "$boots"
done
