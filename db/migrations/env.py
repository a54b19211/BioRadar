from alembic import context
from bioradar.api.config import Settings
from bioradar.data.database import Base, create_database
from bioradar.ops.models import InfrastructureProbe  # noqa: F401
from bioradar.research.engine import models as engine_models  # noqa: F401
from bioradar.research.history import service as history_models  # noqa: F401
from bioradar.research.investigations import service as investigation_models  # noqa: F401
from bioradar.research.monitor import models as monitor_models  # noqa: F401
from bioradar.topics import models as topic_models  # noqa: F401

engine = create_database(Settings())
if engine is None:
    raise RuntimeError("BIORADAR_DATABASE_URL is required for migration")
with engine.connect() as connection:
    context.configure(connection=connection, target_metadata=Base.metadata)
    with context.begin_transaction():
        context.run_migrations()
engine.dispose()
