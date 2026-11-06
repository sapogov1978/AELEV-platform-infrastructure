from sqlalchemy import create_engine

from alembic import context
from translator_service.environment import database_url

engine = create_engine(database_url(), hide_parameters=True)
with engine.connect() as connection:
    context.configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()
