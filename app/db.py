import os

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

load_dotenv()

# Pool settings, and why each one is here.
#
# The database is Neon on the free tier, which scales to zero, sitting behind
# PgBouncer, reached over TLS from a Fly machine that itself suspends when idle.
# Every one of those drops idle connections without telling this end. The pool
# then hands out a socket whose far side is long gone, and the request that
# receives it dies with:
#
#   psycopg.OperationalError: consuming input failed: could not receive data
#   from server: Connection timed out / SSL SYSCALL error
#
# Which is exactly what two visitors got -- one of them on their first page
# load, so the site was simply broken for them. Nothing in the application is
# wrong when this happens; the connection was dead before the query was written.
#
# pool_pre_ping issues a trivial statement before lending a connection out and
# transparently discards it if that fails. It costs one round trip on checkout
# and removes this failure entirely -- and against a database ~75ms away, one
# extra round trip is far cheaper than a 500.
#
# pool_recycle bounds how stale a connection can get in the first place. Neon
# suspends after roughly five minutes idle, so anything older than that is
# suspect; retiring at 240s means the pre-ping rarely has to catch anything.
engine = create_engine(
    os.environ["DATABASE_URL"],
    pool_pre_ping=True,
    pool_recycle=240,
    # The prefetch means one active visitor issues ~150 requests a minute, each
    # taking a connection for the length of a query. The default pool of 5 + 10
    # overflow was sized for a quieter client than this one turned out to be.
    pool_size=10,
    max_overflow=15,
    # TCP keepalives, so a connection idling behind NAT is kept warm rather than
    # silently reaped -- and so a genuinely dead one surfaces in seconds instead
    # of hanging until the OS gives up, which is what "Connection timed out"
    # above actually was.
    connect_args={
        "keepalives": 1,
        "keepalives_idle": 30,
        "keepalives_interval": 10,
        "keepalives_count": 3,
    },
)

# A factory, not a session. Calling Session() makes one; the engine (and its
# connection pool) is shared by all of them.
Session = sessionmaker(engine)
