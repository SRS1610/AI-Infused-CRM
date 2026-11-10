"""The metadata ORM.

Public surface::

    from tessera.orm import Model, fields, api
    from tessera.orm.domain import Domain

Models are declared as Python classes but registered as metadata (``core.model`` /
``core.model_field``) so the client, Studio, the API generator and AI tools can introspect
them. Other modules extend a model with ``_extend = "module.model"`` without forking it.
"""

from tessera.orm import api, fields
from tessera.orm.environment import Environment
from tessera.orm.model import Model

__all__ = ["Environment", "Model", "api", "fields"]
