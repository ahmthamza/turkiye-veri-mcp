"""Client for the TUIK data portal catalog (veriportali.tuik.gov.tr).

The portal exposes a JSON theme tree at /api/{lang}/data/statistical-themes.
Access requires a cookie session established by first visiting the landing
page, plus browser-like headers. Endpoint behaviour discovered via the
tuikr R package (github.com/emraher/tuikr, MIT).
"""

from __future__ import annotations

from typing import Any

import time

import httpx

PORTAL_BASE = "https://veriportali.tuik.gov.tr"

RESOURCE_TYPES = ("dataflow", "istab", "database", "press", "report")

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

_ACCEPT_LANGUAGE = {
    "tr": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
    "en": "en-US,en;q=0.9,tr-TR;q=0.8,tr;q=0.7",
}


class PortalError(RuntimeError):
    """Raised when the TUIK portal returns an error or unexpected payload."""


def _validate_lang(lang: str) -> str:
    if lang not in ("tr", "en"):
        raise ValueError("lang must be 'tr' or 'en'")
    return lang


class PortalClient:
    """Fetches and caches the TUIK portal theme tree."""

    #: Catalog cache lifetime. Keeps a long-running (hosted) server from
    #: serving a stale theme tree after TUIK publishes new tables.
    CACHE_TTL_SECONDS = 3600.0

    def __init__(self, timeout: float = 60.0) -> None:
        self._timeout = timeout
        self._tree_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}

    def theme_tree(self, lang: str = "tr", refresh: bool = False) -> list[dict[str, Any]]:
        lang = _validate_lang(lang)
        cached = self._tree_cache.get(lang)
        if cached and not refresh and time.monotonic() - cached[0] < self.CACHE_TTL_SECONDS:
            return cached[1]

        page_url = f"{PORTAL_BASE}/{lang}/statistical-themes"
        api_url = f"{PORTAL_BASE}/api/{lang}/data/statistical-themes"
        base_headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": _ACCEPT_LANGUAGE[lang],
            "User-Agent": _UA,
        }

        with httpx.Client(
            timeout=self._timeout, follow_redirects=True, headers=base_headers
        ) as client:
            # Step 1: landing page sets session cookies.
            landing = client.get(page_url)
            landing.raise_for_status()
            # Step 2: JSON API call with the session cookies.
            response = client.get(
                api_url,
                headers={
                    "Referer": page_url,
                    "Origin": PORTAL_BASE,
                    "X-Requested-With": "XMLHttpRequest",
                },
            )
            response.raise_for_status()

        try:
            payload = response.json()
        except ValueError as exc:
            raise PortalError(
                "TUIK portal did not return JSON; the endpoint may have changed."
            ) from exc

        if payload.get("isError"):
            raise PortalError(f"TUIK API error: {payload.get('message')}")

        tree = payload.get("data")
        if not isinstance(tree, list):
            raise PortalError("Unexpected TUIK portal payload shape.")

        self._tree_cache[lang] = (time.monotonic(), tree)
        return tree

    def list_themes(self, lang: str = "tr") -> list[dict[str, str]]:
        return [
            {"theme_id": str(node.get("id")), "theme_name": str(node.get("name"))}
            for node in self.theme_tree(lang)
        ]

    def list_resources(
        self,
        theme_id: str,
        lang: str = "tr",
        types: tuple[str, ...] = ("dataflow", "istab"),
    ) -> list[dict[str, Any]]:
        bad = [t for t in types if t not in RESOURCE_TYPES]
        if bad:
            raise ValueError(f"Unknown resource type(s): {bad}. Valid: {RESOURCE_TYPES}")

        tree = self.theme_tree(lang)
        theme_node = next(
            (n for n in tree if str(n.get("id")) == str(theme_id)), None
        )
        if theme_node is None:
            valid = ", ".join(f"{t['theme_id']}={t['theme_name']}" for t in self.list_themes(lang))
            raise ValueError(f"Unknown theme_id '{theme_id}'. Valid themes: {valid}")

        rows: list[dict[str, Any]] = []
        for node in _collect_by_icon(theme_node.get("children") or [], set(types)):
            raw_url = str(node.get("url") or "")
            resource_type = str(node.get("icon"))
            rows.append(
                {
                    "theme_id": str(theme_node.get("id")),
                    "theme_name": str(theme_node.get("name")),
                    "name": str(node.get("name")),
                    "type": resource_type,
                    "dataflow_id": _extract_dataflow_id(raw_url)
                    if resource_type == "dataflow"
                    else None,
                    "url": _absolute_url(raw_url),
                }
            )
        return rows

    def search(
        self,
        query: str,
        lang: str = "tr",
        types: tuple[str, ...] = ("dataflow", "istab"),
    ) -> list[dict[str, Any]]:
        needle = query.casefold()
        hits: list[dict[str, Any]] = []
        for theme in self.list_themes(lang):
            for row in self.list_resources(theme["theme_id"], lang=lang, types=types):
                if needle in str(row["name"]).casefold():
                    hits.append(row)
        return hits


def _collect_by_icon(
    nodes: list[dict[str, Any]], target_icons: set[str]
) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for node in nodes:
        if node.get("icon") in target_icons:
            found.append(node)
        children = node.get("children")
        if children:
            found.extend(_collect_by_icon(children, target_icons))
    return found


def _absolute_url(raw_url: str) -> str:
    if raw_url.startswith(("http://", "https://")):
        return raw_url
    return f"{PORTAL_BASE}{raw_url}"


def _extract_dataflow_id(raw_url: str) -> str:
    return raw_url.rstrip("/").rsplit("/", 1)[-1]
