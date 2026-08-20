# CourseFinder — Build Planner

Natural-language course search over McGill's catalogue, with live seat and
waitlist data. Free for students, so the architecture is built around keeping
the token bill near zero.

---

## Current state

| Piece | Status |
|---|---|
| VSB collector (`collector/vsb_collect.py`) | **Done** — batched httpx sweep, 3,756/3,793 Fall courses in ~100s |
| Loader (`load_observations.py`) | **Done** — XML → `current_seats` + `seat_daily`, ~2s |
| Catalogue loader (`load_courses.py`) | **Done** — 10,156 courses / 9,675 instructors / 15,389 sections |
| Postgres schema | 5 tables + 1 view, 4 migrations, head applied |
| Sweep script (`scripts/collect.sh`) | **Done** — collect + load, with lock and DB check |
| Scheduler | **Deferred** — running `./scripts/collect.sh` manually once a day |
| Search | Not started ← **next** |
| API | `main.py` is still the FastAPI hello-world |
| Frontend / tests / deploy | Not started |

```
courses            10,156
course_instructors  9,675
sections           15,389
current_seats       7,474     one row per section, overwritten each sweep
seat_daily          7,522     one row per section per day
```

---

## Data flow

```
courses-2026-2027.json ──> load_courses.py ──────> courses
                                                   course_instructors
                                                   sections

VSB /api/class-data ──> vsb_collect.py ──> data/raw/202609/batch_NNN.xml
                                                   │
                                                   └──> load_observations.py
                                                              │
                                                              ├──> current_seats
                                                              └──> seat_daily
                                                                       │
                                                                  section_movement (view)
```

`collector/parse_vsb.py` is no longer in the load path — the loader imports its
`parse_class_data()` directly. Its CLI (`--stdout`) is still useful for
eyeballing a sweep.

---

## Schema notes worth remembering

**`sections` is keyed `(crn, term)`, not `crn`.** McGill reuses CRNs across
terms — 6,240 of 8,163 appear in more than one. CRN 516 is `AAAA-100` in Fall
and `WCOM-642` in Summer.

**Location lives inside `sections.meetings`, not on the section.** 224 sections
meet in more than one room; the catalogue's "block" is a meeting pattern, not a
section.

**Instructors are at `(course, term)` grain.** Neither source says which
instructor teaches which section, and it isn't inferable — 1,141 courses list
more instructors than they have lecture sections.

**`current_seats` vs `seat_daily`.** Sweep frequency and history size are
decoupled: `seat_daily` is keyed on `(crn, term, day)`, so sweeping 12×/day
still writes one row per section per day. ~155 bytes/row measured → **~24 MB per
three weeks**, ~140 MB per term.

**Deltas are derived, not stored.** `section_movement` computes 1/3/7-day
movement from snapshots via `LEFT JOIN LATERAL … LIMIT 1` on "most recent
snapshot at or before N days ago" — not an exact-date join, because sweeps get
missed and an exact join silently reports zero movement.

---

## Stack

Package manager is **uv** — `uv add <pkg>`, never `pip install`.

### Backend — installed

| Technology | Role |
|---|---|
| Python >=3.14 | |
| FastAPI >=0.141.1 | HTTP layer |
| SQLAlchemy >=2.0.52 | ORM — **2.0 style** (`Mapped[]`, `select()`), not legacy `query()` |
| Alembic >=1.19.1 | Migrations |
| psycopg[binary] >=3.3.4 | Postgres driver — psycopg **3** |
| python-dotenv >=1.2.2 | `.env` loading |
| Playwright >=1.62.0 | One page load per sweep, to capture VSB's `t`/`e` tokens |
| PostgreSQL 16 (Docker) | Dev on 5435; test on 5433 (needs moving) |

⚠️ **`httpx` is used directly by the collector but isn't declared** — it arrives
transitively via `fastapi[standard]`. Run `uv add httpx` so it can't vanish.

### Backend — to add, by phase

| Technology | Phase | Command |
|---|---|---|
| `anthropic` | 2 | `uv add anthropic` |
| `apscheduler` | 3 | `uv add apscheduler` |
| `fastapi-pagination` | 3 | `uv add fastapi-pagination` |
| `pytest`, `httpx` | 4 | `uv add --dev pytest` |
| `factory-boy`, `faker` | 4 | `uv add --dev factory-boy faker` |
| `slowapi` | 6 | `uv add slowapi` |
| `ruff`, `pre-commit` | any | `uv add --dev ruff pre-commit` |

Pydantic v2 arrives with FastAPI. `cellar_api` is pinned to Pydantic **1.10** —
knowing both is an interview asset.

### Frontend — Phase 5

Vue 3 (Composition API, `<script setup>`), Vite, TypeScript, Pinia, vue-router,
axios, Chart.js + vue-chartjs, Vitest, ESLint.

Scaffold: `npm create vue@latest` (TypeScript, Router, Pinia, Vitest, ESLint),
then `npm i axios chart.js vue-chartjs`.

Deliberately **not** using the `ci_design_system` monorepo (pnpm/turbo/
changesets), Bootstrap, or Storybook. A `tokens.css` file gets the same
vocabulary without the overhead.

---

## Phase 0 — Data collection ✅ mostly done

**Done:** the collector sweeps every Fall course from the catalogue in ~100s;
the loader writes both seat tables in ~2s; `scripts/collect.sh` runs the pair
with a lock, a Postgres reachability check, and log rotation.

**Remaining:** install a scheduler. Deferred by choice — running
`./scripts/collect.sh` manually once a day is enough, since `seat_daily` only
needs one successful run per day.                      
      
When you do schedule it, prefer **launchd** over cron on a laptop: cron silently
skips jobs while the Mac sleeps, launchd re-runs them on wake. macOS also
requires granting Full Disk Access to `/usr/sbin/cron`. Plist and crontab line  
are in the header of `scripts/collect.sh`.

---

## Phase 1 — Search core (2–3 days) ← next, the money milestone

Pure SQL. No LLM in this phase.

**Build:** `search_courses(subject, credits, term, max_difficulty, keywords,
has_open_seats, limit)` returning ranked rows.

**Techniques to learn:**

| Technique | Why it matters here |
|---|---|
| `LEFT JOIN LATERAL … ORDER BY … LIMIT 1` | "Nearest row per outer row" — already used in `section_movement`; understand it before extending it |
| Composite indexes | `(crn, term, day DESC)` exists; you'll want more once search queries take shape |
| Partial indexes | `WHERE open_seats > 0` — small index, very common filter |
| `pg_trgm` + GIN index | Fuzzy title/description matching **without embeddings**. `CREATE EXTENSION pg_trgm;` — try this before Phase 7 |
| `EXPLAIN (ANALYZE, BUFFERS)` | Seq scan vs index scan vs bitmap heap scan |
| SQLAlchemy 2.0 `select()`, `join()`, `func`, `.label()` | Expressing the above in the ORM |
| N+1 queries / `selectinload()` | Fetching sections per course |

Searching for "has seats" reads `current_seats` — a flat 7.5k-row table — so no
query has to find a maximum `observed_at` across history. That was the point of
the split.

**Read:** SQLAlchemy 2.0 *ORM Querying Guide*; PostgreSQL *Using EXPLAIN* and
*Indexes*; **Use The Index, Luke**.

**Done when:** one query is measurably faster after an index, and you can
explain why the planner changed its mind.

---

## Phase 2 — LLM filter layer — **DEFERRED**

Built once, then removed: `app/query.py`, the `query_cache` table, and the
`anthropic` dependency are all gone. Filters-only for now; the chips are the
whole input surface.

The economics said it wasn't worth keeping yet: the regex and cache tiers meant
the model only fired on genuinely novel phrasings, so a whole term came to a few
dollars — and a cheaper provider would have saved single-digit dollars for real
engineering work. If you want fewer model calls, the lever is **extending the
deterministic keyword extractor**, not swapping providers:

```
"3 credit" → credits=3        "easy"       → max_difficulty=2.5
"comp"     → subject=COMP     "with seats" → has_open_seats=True
"200 level"→ min/max_level    "downtown"   → campus=Downtown
```

Those are keyword matches — free, instant, no dependency. A model is only
needed for the fuzzy tail ("something creative that isn't too much work").

The design below is preserved for when it comes back. Only `from_model` was
provider-specific; everything else is reusable.

### Original design (for reference)

Question → filter object. The LLM writes the **query**, never the answer — a
20–50× cost difference.

```
"easy 3-credit courses with seats left"
        ↓ regex fast path? → cache hit? → else one Haiku call
{"credits": 3, "max_difficulty": 2.5, "has_open_seats": true}
        ↓ fills the Phase 5 filter chips (user can adjust, free)
        ↓ Phase 1's search_courses()
[ranked cards, rendered by Vue]
```

The output shape **is** the chip state — see Phase 5. That makes the prompt
easy ("emit this JSON") and the test trivial ("do the right chips light up?"),
and it means a user who clicks instead of typing never triggers an API call.

| Technology | Detail |
|---|---|
| `anthropic` SDK | `client.messages.parse()` — structured outputs |
| `claude-haiku-4-5` | $1/M in, $5/M out. A parsing task, not a reasoning task |
| Pydantic model as schema | `messages.parse()` validates into it |
| `re` fast path | Course codes, subject codes, bare "seats" queries |
| Postgres cache table | `question_hash → filter_json` |

**Cost controls, non-negotiable:** `max_tokens=150`; structured outputs **not**
tool use (one round trip, not two); cache by normalized question; regex before
cache before API.

**Gotchas:** Haiku 4.5 needs a **4,096-token prefix** before prompt caching
engages (Opus 5 caches from 512) — yours will be far under, so caching silently
does nothing; that's fine at $1/M. And `temperature`/`top_p`/`top_k` return a
**400** on current models — steer with the prompt.

**Done when:** ten varied questions produce correct filters, and re-asking any
of them makes zero API calls.

---

## Phase 3 — API layer (3 days)

The `cellar_api` mirror — the phase with the most interview material.

```
app/
  routes/        HTTP verbs, status codes, response_model
  controllers/   orchestrate + transform
  services/      business logic
  repositories/  BaseRepository[T] + per-entity queries
  models/        SQLAlchemy (exists)
  schemas/       Pydantic Create/Update/Response
  transformers/  model → response shaping
  jobs/          APScheduler sweep job (replaces the manual script)
  config/        db, settings
  dependencies/  DI
```

```
POST /search                  question → ranked courses
GET  /courses/{id}            detail + sections + instructors
GET  /sections/{crn}/movement seat history + deltas for the chart
```

| Concept | Where |
|---|---|
| `Depends()` DI | FastAPI docs — *Dependencies* |
| Router composition | FastAPI docs — *Bigger Applications* |
| `response_model` + `from_attributes` | Pydantic v2 docs |
| Generic `BaseRepository[T]` | `TypeVar`, `Generic` — read `cellar_api/app/repositories/base_repository.py`, then rewrite from memory |
| APScheduler + a DB lock | Prevents double-runs across workers |

**Done when:** `/search` answers a natural-language question end to end.

---

## Phase 4 — Tests (2 days)

pytest, factory-boy, Faker, httpx `TestClient`. Structure mirrors the layer tree,
plus one `tests/integration/test_search_wire_contract.py`.

**Learn:** pytest fixtures and `conftest.py` scoping, transactional rollback
between tests, factory-boy `SubFactory` / `Sequence`.

**First:** move `db_test` off port 5433 — `cellar_test_db` occupies it. Use 5434.

**Done when:** tests pass against a database wiped between runs.

---

## Phase 5 — Frontend (4–5 days)

Filter chips + chat input → result cards → seat-movement chart.

### The LLM populates the chips, it does not bypass them

Both input paths land on the same `search_courses()` kwargs, so the filter UI is
the contract:

```
"easy 3-credit courses with seats"
        ↓ Haiku (one call)
[3 credits ×] [difficulty ≤ 2.5 ×] [has seats ×] [undergrad ×]
        ↓ user adjusts any chip -- zero further API calls
search_courses(credits=3, max_difficulty=2.5, has_open_seats=True,
               undergrad_only=True)
```

Three reasons this beats sending the sentence straight to a search endpoint:

- The user **sees** what was understood, so a misparse is obvious rather than
  mysterious
- They fix it by **clicking**, not retyping — every adjustment after the first
  is free
- The product works with **zero LLM calls** for anyone who clicks their way to
  an answer, which is the cheapest possible path

**Build the chips before the chat box.** Phase 2's prompt then has a much easier
job ("emit this JSON shape") and a trivial test ("does the right chip set light
up for this sentence?").

### Filter → control mapping

Cardinalities measured against Fall 2026 offerings (3,793 courses):

| Filter | Distinct values | Control |
|---|---|---|
| `credits` | `3` alone covers 2,423 | Chips: 3 · 4 · 6 · 1 · other |
| `campus` | 4 — Downtown, Macdonald, Distance, Off-campus | Chips (Macdonald is 30km away, this matters) |
| `undergrad_only` | boolean | Toggle, **on by default** |
| `has_open_seats` | boolean | Toggle |
| `min_level` / `max_level` | 100–900 | Chips: 100 · 200 · 300 · 400 |
| `faculty` | 20 | Dropdown |
| `department` | 109 | Searchable select |
| `subject` | 249 | Searchable select — too many for chips |
| `max_difficulty` / `min_rating` | continuous | Sliders |
| `title` / `keywords` / `instructor` | unbounded | Text inputs |
| `max_waitlist` | continuous | Slider or numeric |

"Graduate Studies" is the single largest faculty at 1,431 courses — ~40% of Fall
offerings — which is why `undergrad_only` defaults on. The real search space for
your users is ~2,400 courses.

Show the hidden count somewhere (*"1,074 reviewed · 2,719 without reviews
hidden"*) so the reviewed-only boundary is visible rather than mysterious.

### Vue specifics

| Concept | Detail |
|---|---|
| Composition API + `<script setup>` | Vue 3 docs — *Essentials* |
| `ref` vs `reactive` vs `computed` | The thing everyone gets wrong first |
| Pinia store | Chip state is the store; results derive from it |
| axios instance + interceptors | Base URL, error handling |
| vue-chartjs `<Line>` | Waitlist movement over days |
| Vite proxy | `/api` → `localhost:8000`, avoids CORS in dev |

Results render as **cards, not prose** — that's what keeps the bill near zero.

**Done when:** clicking chips returns cards with no API call, and typing "easy
3-credit courses with seats left" fills those same chips.

---

## Phase 6 — Deploy + abuse controls (2 days)

A public endpoint with no auth becomes someone's free LLM proxy within a week.

- Per-session/IP rate limit (`slowapi`), ~20 questions/hour
- `max_tokens=150` hard cap on the filter call
- Anthropic workspace spend limit as a backstop
- Dockerfile + compose for the app; APScheduler takes over the sweep
- Deploy: Railway / Fly.io / Render (managed Postgres included)

**Done when:** a hostile user can't run up your bill.

---

## Phase 7 — Optional: semantic search

Only once real questions arrive that the filter layer can't answer.

- `pgvector` + a `Vector` column on `courses`, HNSW index, cosine distance
- Embeddings from a non-Anthropic provider — **Anthropic has no embeddings
  endpoint**. Voyage AI (hosted) or `sentence-transformers` (local, free)
- Hybrid ranking: vector distance combined with the structured filters

**Try `pg_trgm` from Phase 1 first.** It may be enough.

---

## Timeline

**~2.5 weeks of working days** from here. Phases 1 and 3 are the ones to slow
down on. Treat **Phases 0–3 as the shippable product** — a working `/search` you
can demo is a complete thing; the frontend is a bonus.

---

## Open decisions

- [ ] **HTML entities in text columns** — `faculty` reads `Faculty of Medicine &amp; Hlth Sci`. The catalogue is HTML-escaped and `to_course_fields` passes it through. Fix with `html.unescape()` + a reload; affects `faculty`, `department`, and probably some titles/descriptions. Will show up literally in the Phase 5 faculty dropdown.
- [ ] `uv add httpx` — used directly, currently only a transitive dependency
- [ ] Delete 7 stale flat XMLs in `data/raw/` (pre-rewrite layout, re-parsed every run; harmless but wasteful)
- [ ] Sweep Winter 2027 (`--term 202701`) too, or Fall only until it ships
- [ ] Rename `load_courses.py` → `load_catalogue.py` (loads three tables)
- [ ] Move `db_test` from 5433 → 5434
- [ ] Verify the Summer term code (`202605`) against a live VSB response
- [ ] Prerequisite graph — `prerequisites`, `corequisites`, `leadingTo`, `logicalPrerequisites` are all in the catalogue JSON but **not loaded**. Would need a `course_prerequisites` edge table and recursive CTEs to traverse ("what can I take next"). Later phase.

**Resolved:** `avg_rating` 0.0 vs `NULL` — unreviewed courses store `NULL`,
applied by `update_ratings.py`.

**Resolved:** history granularity (current + daily snapshot, not every
observation); JSONL intermediate dropped; unknown-course policy (skip and log).

---

## Command reference

```bash
# The whole scheduled job
./scripts/collect.sh                      # ~2 min: sweep + load

# Or the pieces
.venv/bin/python collector/vsb_collect.py           # full Fall sweep, ~100s
.venv/bin/python collector/vsb_collect.py --term 202701
.venv/bin/python collector/vsb_collect.py --courses COMP-250   # targeted, keeps the sweep
.venv/bin/python collector/vsb_collect.py --limit 100          # testing
.venv/bin/python load_observations.py               # XML -> both seat tables
.venv/bin/python load_courses.py                    # reload the catalogue (idempotent)

# Migrations
.venv/bin/python -m alembic revision --autogenerate -m "..."
.venv/bin/python -m alembic upgrade head
.venv/bin/python -m alembic downgrade -1

# Database
docker compose up -d db
psql "$DATABASE_URL"                      # credentials live in .env only
```

**Always read an autogenerated migration before applying it.** Alembic does not
detect primary key changes and emits drop+add pairs where you meant a rename —
it has already been wrong once on this project.

**Don't pipe alembic through `tail` in a script.** The pipeline's exit status is
`tail`'s, so a failed migration looks like it succeeded. Use `set -o pipefail`.
