import os

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

load_dotenv()

engine = create_engine(os.environ["DATABASE_URL"])

# A factory, not a session. Calling Session() makes one; the engine (and its
# connection pool) is shared by all of them.
Session = sessionmaker(engine)
