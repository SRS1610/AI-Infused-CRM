"""web — menus, actions, views as data and the client metadata API."""

from modules.web import api as _api  # noqa: F401 - registers the router
from modules.web import loaders as _loaders  # noqa: F401 - registers the views/menus/actions loaders
from modules.web import search as _search  # noqa: F401 - registers the search index hooks
