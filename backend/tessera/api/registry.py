"""Modules contribute API routers with ``@api_router`` at import time (the app factory mounts them)."""

from __future__ import annotations

from fastapi import APIRouter

_routers: list[APIRouter] = []


def register_router(router: APIRouter) -> APIRouter:
    if router not in _routers:
        _routers.append(router)
    return router


def contributed_routers() -> list[APIRouter]:
    return list(_routers)
