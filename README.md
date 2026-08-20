Objective: Provide users with a prompt based interface where they can search for courses
through specific language such as "easy" "4 credit" "Italian" or maybe a specific topic 
they want to learn about or even just what there degree is and what are their options. 

Specs:

Input: Either a sentence they type out or a grill me to search for the next course. 
Output: A list of courses that fit under the users request, ranked by relevance to the prompt. 
Data needed: All courses and for each: their semester available, their professor, credit number,
background on the course from reddit 

Internship pattern	CourseFinder equivalent
LoRaWAN sensor ingest → Postgres/Influx	Seat observations → Postgres. Same shape: timestamped readings from an external source.
Distributed job locks, idempotent processing	Prevent overlapping collector runs; don't double-insert the same observation
APScheduler threshold-breach alerting	Seat alerts. Threshold = seats > 0. Same pattern exactly.
Twilio SMS + email, per-user channel preferences	Alert delivery, user picks email or SMS
Layered route → controller → service → repository	Your search/alerts API
SQLAlchemy 2.0 + Alembic, 30-table schema	courses / sections / observations / subscriptions
JWT + Google OAuth + permissions	Accounts for managing alerts
Vue 3 + Pinia + Chart.js time-series	Seat-history charts — Chart.js is made for your fill-rate data
pytest + factory_boy + moto	Tests for the pipeline
