#!/bin/bash
#
# What real visitors are actually getting.
#
# A working page view is one /courses request followed by detail prefetches. A
# page view with / and /courses/filters but NO /courses is the failure worth
# hunting: the browser never issued the list request, so the page sits on a
# skeleton. That pattern is invisible from the outside -- every logged request
# returns 200 -- and only shows up as an absence.
APP="${1:-mcgill-course-finder}"

log=$(fly logs -a "$APP" --no-tail 2>/dev/null | grep -v '/health')

printf 'status codes seen:\n'
printf '%s\n' "$log" | grep -oE '" [0-9]{3} ' | tr -d '" ' | sort | uniq -c | sed 's/^/  /'

printf '\nrequests by endpoint:\n'
printf '  %-28s %s\n' "GET /"          "$(printf '%s\n' "$log" | grep -c 'GET / HTTP')"
printf '  %-28s %s\n' "/courses (list)" "$(printf '%s\n' "$log" | grep -c 'GET /courses?')"
printf '  %-28s %s\n' "/courses/filters" "$(printf '%s\n' "$log" | grep -c 'GET /courses/filters')"
printf '  %-28s %s\n' "/courses/{id} (detail)" "$(printf '%s\n' "$log" | grep -cE 'GET /courses/[A-Z]')"

# Compare filters against courses, NOT "GET /" against courses. A curl, a
# smoke test or a link preview fetches / and never runs the script, so counting
# those as page views invents failures that did not happen. boot() issues both
# filters and courses, so only a browser that ran the code appears in either --
# and filters arriving without courses is the pattern actually worth hunting.
filters=$(printf '%s\n' "$log" | grep -c 'GET /courses/filters')
lists=$(printf '%s\n' "$log" | grep -c 'GET /courses?')
printf '\n'
if [ "$filters" -gt 0 ] && [ "$lists" -lt "$filters" ]; then
  printf '  %s boots requested filters but only %s requested courses\n' "$filters" "$lists"
  printf '  -> %s visitor(s) never got their list. This is the bug.\n' "$((filters - lists))"
else
  printf '  %s boots, %s list requests -- every browser that loaded got its courses.\n' "$filters" "$lists"
  printf '  (more lists than boots is normal: filter clicks refetch without re-booting)\n'
fi

printf '\n429s (rate limited): %s\n' "$(printf '%s\n' "$log" | grep -c ' 429 ')"
