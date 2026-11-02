from alembic import context

from bioradar import db

with db.engine().connect() as connection:
    context.configure(connection=connection, target_metadata=db.Base.metadata)
    with context.begin_transaction():
        context.run_migrations()
