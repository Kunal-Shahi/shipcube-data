"""ShipCube data layer: a semantic layer whose model lives in a catalog DB and changes through migrations.

Entry points for ShipCube's MCP server and API:

    from shipcube_data import Catalog, DataLayer, DataTools, Caller, MuseDrafter
"""

from .catalog import Catalog
from .compiler import SecurityContext
from .datasources import Datasource, SecretRef, register_datasource
from .drafting import MuseDrafter, RuleDrafter
from .engine import DataLayer
from .identity import Caller, security_context
from .migrate import Runner
from .model import ModelError
from .tools import DataTools, ToolError

__version__ = "0.2.0"
__all__ = [
    "Caller", "Catalog", "DataLayer", "DataTools", "Datasource", "ModelError", "MuseDrafter",
    "RuleDrafter", "Runner", "SecretRef", "SecurityContext", "ToolError", "register_datasource", "security_context",
]
