"""Realtime hub: WebSocket connections subscribed to channels, fed by an in-process or Redis bus.

Channels:

* ``user:<uid>``              notifications for one user (only that user may subscribe)
* ``record:<model>:<id>``     changes to one record (subscriber must be able to read it)
* ``model:<model>``           any change on a model (subscriber needs read access rights)
* ``job:<uuid>`` / ``ai:<run_id>``  progress streams (owner only; used from Phase 1.3 / 1.5)
"""

from tessera.realtime.hub import hub, publish, publish_after_commit

__all__ = ["hub", "publish", "publish_after_commit"]
