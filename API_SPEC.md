# CourseFinder API — build spec

Phase 3. Filters only, no natural-language layer. Build against this.

---

## Design decisions, and why

**`GET /courses` with query params, not `POST /search`.** The filter chips *are*
the query string, so a search is a URL — shareable, bookmarkable, back-button
friendly, and cacheable by any proxy. `POST` for a read makes all of that
impossible. Use `POST` only if a filter set ever outgrows a URL (it won't here).

**Query params map 1:1 to `search_courses()` kwargs.** No translation layer, no
renaming. One name for a concept from the URL to the SQL. When you add a filter,
you add it in `app/search.py` and in the filter model, and nothing in between
needs to know.

**Schemas, not ORM objects, past the controller.** A `Course` returned from a
closed session raises `DetachedInstanceError` the moment anything touches a lazy
attribute — and it will, during JSON serialization. Convert at the controller
boundary.

**No `source` field.** That was for reporting regex/cache/model hit rates. With
no LLM there's nothing to report.

---

## Files

| File | Responsibility | Rough size |
|---|---|---|
| `app/main.py` | `FastAPI()`, CORS, router registration | 20 lines |
| `app/dependencies/db.py` | `get_session()` generator for `Depends()` | 10 lines |
| `app/schemas/courses.py` | `CourseFilters`, `CourseSummary`, `CourseDetail`, `SearchResponse` | 80 lines |
| `app/schemas/sections.py` | `SectionSummary`, `MovementResponse` | 40 lines |
| `app/repositories/course_repository.py` | Wraps `app/search.py`; adds `get_by_id`, `filter_options` | 60 lines |
| `app/repositories/section_repository.py` | Movement query against `section_movement` | 40 lines |
| `app/services/course_service.py` | Business logic: defaults, the reviewed-only rule | 40 lines |
| `app/controllers/course_controller.py` | ORM → schema, assemble `SearchResponse` | 50 lines |
| `app/routes/courses.py` | HTTP verbs, status codes, `response_model` | 50 lines |
| `app/routes/sections.py` | Same for movement | 30 lines |

`app/search.py` stays where it is — the repository calls it. Don't move the query
logic into the repository; it's already tested where it lives.

Delete the root `main.py` hello-world once `app/main.py` works.

---

## `GET /courses`

Search. Every parameter optional.

### Query parameters

Use a Pydantic model as the query-param container (FastAPI ≥0.115 supports this),
so the filter surface is declared once:

```python
@router.get("")
def search(filters: Annotated[CourseFilters, Query()],
           session: Session = Depends(get_session)):
```

| Param | Type | Notes |
|---|---|---|
| `subject` | `str` | `COMP`. Uppercased downstream |
| `course_id` | `str` | `COMP-250` |
| `title` | `str` | Substring of the course name |
| `keywords` | `str` | Matches name **or** description |
| `credits` | `float` | Exact credit count |
| `min_level` / `max_level` | `int` | 100–900 |
| `undergrad_only` | `bool` | **default `true`** |
| `faculty` / `department` | `str` | Substring |
| `instructor` | `str` | Surname substring; term-scoped |
| `max_difficulty` | `float` | `ge=1, le=5` |
| `min_rating` | `float` | `ge=1, le=5` |
| `min_reviews` | `int` | `ge=0` |
| `reviewed_only` | `bool \| None` | `null` = auto (see below) |
| `term` | `str` | default `202609` |
| `campus` | enum | `Downtown \| Macdonald \| Distance \| Off-campus` |
| `class_type` | `str` | `Lec`, `Tut`, … |
| `has_open_seats` | `bool` | default `false` |
| `max_waitlist` | `int` | `ge=0` |
| `offered_only` | `bool` | default `true` |
| `sort_by` | enum | `rating \| easiest \| popular \| seats` |
| `limit` | `int` | `ge=1, le=100`, default 50 |
| `offset` | `int` | `ge=0`, default 0 |

Put the bounds in `Field(ge=..., le=...)`. FastAPI then returns **422** with a
useful message for `max_difficulty=99`, and you never have to validate in the
service.

### Response — 200

```json
{
  "total": 151,
  "limit": 50,
  "offset": 0,
  "hidden_unreviewed": 2719,
  "courses": [
    {
      "id": "ANAT-321",
      "subject": "ANAT",
      "code": "321",
      "title": "Circuitry of the Human Brain",
      "credits_text": "3",
      "faculty": "Faculty of Science",
      "department": "Anatomy and Cell Biology",
      "avg_rating": 4.98,
      "avg_difficulty": 2.47,
      "review_count": 938,
      "url": "https://coursecatalogue.mcgill.ca/courses/anat-321"
    }
  ]
}
```

`total` comes from `count_matching()` — the same filters, `SELECT count(*)`, no
rows fetched. Do **not** derive it from `len(courses)`; that's the page size.

`hidden_unreviewed` is `count_matching(reviewed_only=False) - total` when the
reviewed-only rule is active, else `0`. This is what makes the boundary visible
in the UI instead of mysterious. Two counts per search is fine.

---

## `GET /courses/{course_id}`

`404` if no such course.

```json
{
  "id": "COMP-250",
  "title": "Introduction to Computer Science",
  "description": "...",
  "credits_text": "3",
  "credits_min": 3.0,
  "credits_max": 3.0,
  "faculty": "Faculty of Science",
  "avg_rating": 3.86,
  "avg_difficulty": 3.18,
  "review_count": 3941,
  "instructors": ["Hsiu-Chin Lin", "Faten M'hiri"],
  "sections": [
    {
      "crn": "2318",
      "section_no": "001",
      "type_of_class": "Lec",
      "campus": "Downtown",
      "meetings": [{"day": 3, "start_min": 695, "end_min": 775, "location": "MCMED 522"}],
      "open_seats": 29,
      "waitlist_count": 80,
      "is_full": false,
      "observed_at": "2026-08-18T19:41:27Z"
    }
  ]
}
```

`instructors` is term-scoped — filter `course_instructors` by the requested term
or you'll list next year's staff.

**Use `selectinload` for sections**, or you'll issue one query per section. One
course makes this invisible; it's the habit that matters.

---

## `GET /sections/{crn}/movement?term=202609`

Reads the `section_movement` view. `404` if the `(crn, term)` pair is unknown.

```json
{
  "crn": "3784",
  "term": "202609",
  "current": {"open_seats": 42, "waitlist_count": 0, "observed_at": "..."},
  "deltas": {
    "wl_delta_1d": -3,
    "wl_delta_7d": -12,
    "days_span_7d": 7,
    "wl_cleared_per_day": 1.71
  },
  "history": [
    {"day": "2026-08-18", "open_seats": 42, "waitlist_count": 12},
    {"day": "2026-08-19", "open_seats": 39, "waitlist_count": 9}
  ]
}
```

`deltas` will be `null` for the first week — that's correct, not a bug. Surface
`days_span_7d` alongside so the UI can say "12 spots over 6 days" rather than
implying an exact window.

`history` is the chart series: `SELECT day, open_seats, waitlist_count FROM
seat_daily WHERE crn=… AND term=… ORDER BY day`.

---

## `GET /filters`

Populates the chips and dropdowns. Cheap, and it only changes when the catalogue
reloads — cache it in the frontend for the session.

```json
{
  "faculties": ["Faculty of Arts", "Faculty of Science", "..."],
  "subjects": [{"code": "COMP", "count": 61}],
  "campuses": ["Downtown", "Macdonald", "Distance", "Off-campus"],
  "credit_options": [3, 4, 6, 1, 2],
  "levels": [100, 200, 300, 400],
  "terms": [{"code": "202609", "label": "Fall 2026"}]
}
```

Scope every option list to courses **offered in the requested term** — a
dropdown offering 249 subjects when only 180 are offered this Fall produces
empty result sets and looks broken.

---

## Gotchas

**`Decimal` serializes as a JSON string in Pydantic v2.** `credits_min` will come
out as `"3.00"`, not `3.0`. Either declare the schema field as `float`, or accept
strings and parse in the frontend. Decide once — a mixed convention is worse than
either choice.

**`reviewed_only` is tri-state.** `true` / `false` / `null`, where `null` means
"let `search_courses` decide" — it drops the reviewed-only default when a subject
or course is named. Type it `bool | None`, default `None`, and don't collapse it
to a plain bool.

**`exclude_none` when forwarding filters.** `search_courses(**filters.model_dump(
exclude_none=True))` — passing `subject=None` explicitly is harmless today but
will bite when a filter's default stops being `None`.

**CORS.** The Vite dev server is a different origin. Either add
`CORSMiddleware` with `allow_origins=["http://localhost:5173"]`, or proxy `/api`
in `vite.config.ts` — the proxy is simpler and matches production.

**Don't put session creation in the route.** `Depends(get_session)` with a
`try/finally` close, so a failed request can't leak a connection.

---

## Done when

1. `GET /courses?credits=3&max_difficulty=2.5&undergrad_only=true` returns
   `total: 151` with `ANAT-321` first — the same result `search_courses` gives
   today.
2. `GET /courses?max_difficulty=99` returns **422**, not a 500.
3. `GET /courses/COMP-250` returns sections with live seat counts, in one query
   per relationship rather than one per row.
4. `GET /courses/NOPE-999` returns **404**, not an empty 200.
5. `/docs` renders every parameter with its bounds — that's the free win from
   declaring them in the schema.

---

## Deliberately out of scope

- **Seat data on search results.** `CourseSummary` has no seat fields, because
  `search_courses` returns `Course` objects and adding aggregates changes its
  return shape. Do it after the basic slice works, as a separate
  `search_with_seats` returning `(Course, max_open_seats, min_waitlist)`.
- **Auth.** One user. Add it later if you want the practice, not because this
  needs it.
- **The scheduler.** Still `scripts/collect.sh` by hand. It becomes an
  APScheduler job when you deploy.
