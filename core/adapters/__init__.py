# Adapters package — each module registers itself via register_adapter()
# Import order here determines registration priority (more specific first).
# Add new platform adapters here so they register before _select_adapter runs.

from core.adapters import workday_adapter     as _workday     # noqa: F401
from core.adapters import amazon_adapter      as _amazon      # noqa: F401
from core.adapters import greenhouse_adapter  as _greenhouse  # noqa: F401
from core.adapters import lever_adapter       as _lever       # noqa: F401
from core.adapters import generic_adapter     as _generic     # noqa: F401
