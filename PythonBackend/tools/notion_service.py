"""
notion_service.py

A higher-level Notion integration layer for Jugnu (or any LLM-driven agent)
to call, sitting on top of the raw Notion REST API. Addresses the review
feedback on the earlier flat-function version:

    1. Structured (dict) return values instead of formatted strings
    2. Name-based resolution, so callers never pass raw UUIDs
    3. A refreshable in-memory workspace cache instead of hitting /search
       on every call
    4. Best-effort "semantic" search -- with an honest caveat, see
       semantic_search()'s docstring
    5. The missing write/admin operations: update, append, archive/restore,
       move, duplicate, comments, file upload, schema retrieval
    6. All of it organized as a single NotionService, so the agent never
       talks to the Notion API directly

SECURITY
--------
The version of this file you shared hardcoded a live-looking integration
token directly in source, despite the docstring saying it should come from
an environment variable. This version reads the token *only* from
NOTION_API_KEY (or a constructor arg, for tests) -- there is no literal
token anywhere in this file. Rotate the token you pasted; treat it as
compromised since it's now sitting in a chat transcript.

NOTES ON THE NEWER ENDPOINTS USED BELOW
----------------------------------------
- archive_page() sends `in_trash` rather than `archived`: as of API version
  2026-03-11, `archived` was removed and `in_trash` is the only accepted
  field for trashing/restoring a page.
- move_page() uses the dedicated POST /pages/{id}/move endpoint (this is a
  distinct endpoint from PATCH /pages/{id}; it did not exist in older API
  versions).
- upload_file() uses the File Upload API: create a file_upload object,
  either send bytes to it (local files) or pass mode="external_url"
  (already-public files), then attach it to a block via a
  {"type": "file_upload", "file_upload": {"id": ...}} file object.

None of this has been run against a live workspace in this environment (no
network path to api.notion.com here) -- it's been syntax-checked and
exercised against mocked HTTP responses instead. Test it against a real
sandbox page before pointing it at anything you care about.
"""

from __future__ import annotations

import os
import time
import difflib
from datetime import date
from typing import Optional

import requests


NOTION_VERSION = "2026-03-11"
NOTION_BASE = "https://api.notion.com/v1"


class NotionService:
    def __init__(self, api_key: Optional[str] = None, cache_ttl_seconds: int = 3600):
        self.api_key = api_key or os.environ.get("NOTION_API_KEY")
        self.cache_ttl_seconds = cache_ttl_seconds
        self._cache: dict[str, dict] = {}   # title -> {"id": ..., "object": "page"|"database"}
        self._cache_loaded_at: float = 0.0

    # ------------------------------------------------------------------
    # low-level plumbing -- every Notion call funnels through _request so
    # every public method returns the same {"success": ...} shape
    # ------------------------------------------------------------------

    def _headers(self) -> Optional[dict]:
        if not self.api_key:
            return None
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, *, json_body: dict = None,
                 params: dict = None, files=None) -> dict:
        headers = self._headers()
        if not headers:
            return {"success": False, "error": "NOTION_API_KEY is not set.", "status": None}

        if files is not None:
            # multipart upload: let `requests` set its own Content-Type w/ boundary
            headers = {k: v for k, v in headers.items() if k != "Content-Type"}

        url = f"{NOTION_BASE}/{path.lstrip('/')}"
        try:
            resp = requests.request(
                method, url, headers=headers, timeout=30,
                json=None if files is not None else json_body,
                params=params, files=files,
            )
        except requests.RequestException as e:
            return {"success": False, "error": str(e), "status": None}

        body = None
        try:
            body = resp.json()
        except ValueError:
            pass

        if not resp.ok:
            message = (body or {}).get("message") if body else None
            return {"success": False, "error": message or resp.text[:500], "status": resp.status_code, "raw": body}
        return {"success": True, "data": body, "status": resp.status_code}

    @staticmethod
    def _extract_title(obj: dict) -> str:
        props = obj.get("properties", {})
        for prop in props.values():
            if prop.get("type") == "title":
                texts = prop.get("title", [])
                if texts:
                    return "".join(t.get("plain_text", "") for t in texts)
        top_title = obj.get("title")
        if isinstance(top_title, list) and top_title:
            return "".join(t.get("plain_text", "") for t in top_title)
        return "(untitled)"

    @staticmethod
    def _plain_text_of_block(block: dict) -> str:
        block_type = block.get("type")
        rich_text = block.get(block_type, {}).get("rich_text", [])
        return "".join(t.get("plain_text", "") for t in rich_text)

    @staticmethod
    def _looks_like_id(s: str) -> bool:
        stripped = s.replace("-", "")
        return len(stripped) == 32 and all(c in "0123456789abcdefABCDEF" for c in stripped)

    # ------------------------------------------------------------------
    # (3) caching + (2) name resolution
    # ------------------------------------------------------------------

    def sync_workspace(self, force: bool = False) -> dict:
        """Rebuild the title -> id/object cache by paging through /search.

        Called automatically by resolve_name_to_id whenever the cache is
        empty or older than cache_ttl_seconds. Call it directly to force a
        refresh -- e.g. from a scheduled job every few hours, or right
        after creating something you need to reference by name immediately.
        """
        now = time.time()
        if not force and self._cache and (now - self._cache_loaded_at) < self.cache_ttl_seconds:
            return {"success": True, "refreshed": False, "entries": len(self._cache)}

        entries: dict[str, dict] = {}
        cursor = None
        while True:
            payload = {"page_size": 100}
            if cursor:
                payload["start_cursor"] = cursor
            result = self._request("POST", "search", json_body=payload)
            if not result["success"]:
                return result
            data = result["data"]
            for obj in data.get("results", []):
                title = self._extract_title(obj)
                if title and title != "(untitled)":
                    entries[title] = {"id": obj["id"], "object": obj.get("object")}
            if data.get("has_more") and data.get("next_cursor"):
                cursor = data["next_cursor"]
            else:
                break

        self._cache = entries
        self._cache_loaded_at = now
        return {"success": True, "refreshed": True, "entries": len(entries)}

    def resolve_name_to_id(self, name_or_id: str) -> dict:
        """Turn a human name ("Projects", "Reflection Engine") into a Notion
        id. Callers should never need to pass a UUID directly, but if one is
        passed anyway it's returned as-is.

        Resolution order: already-an-id -> cache exact match -> cache fuzzy
        match -> a live /search call (covers anything created since the
        cache was last refreshed).
        """
        if self._looks_like_id(name_or_id):
            return {"success": True, "id": name_or_id, "object": None, "match": "already_an_id"}

        if not self._cache:
            sync_result = self.sync_workspace()
            if not sync_result["success"]:
                return sync_result

        if name_or_id in self._cache:
            entry = self._cache[name_or_id]
            return {"success": True, "id": entry["id"], "object": entry["object"], "match": "exact"}

        close = difflib.get_close_matches(name_or_id, self._cache.keys(), n=1, cutoff=0.6)
        if close:
            entry = self._cache[close[0]]
            return {"success": True, "id": entry["id"], "object": entry["object"],
                    "match": "fuzzy", "matched_title": close[0]}

        live = self._request("POST", "search", json_body={"query": name_or_id, "page_size": 5})
        if not live["success"]:
            return live
        results = live["data"].get("results", [])
        if not results:
            return {"success": False, "error": f"No page or database found matching '{name_or_id}'."}
        best = results[0]
        title = self._extract_title(best)
        self._cache[title] = {"id": best["id"], "object": best.get("object")}
        return {"success": True, "id": best["id"], "object": best.get("object"), "match": "live_search"}

    # ------------------------------------------------------------------
    # (1) core CRUD -- structured returns, name-resolved parents
    # ------------------------------------------------------------------

    def search(self, query: str = "", pages_only: bool = False) -> dict:
        payload = {"query": query}
        if pages_only:
            payload["filter"] = {"value": "page", "property": "object"}
        result = self._request("POST", "search", json_body=payload)
        if not result["success"]:
            return result
        items = [
            {"object": r.get("object"), "id": r.get("id"), "title": self._extract_title(r), "url": r.get("url")}
            for r in result["data"].get("results", [])
        ]
        return {"success": True, "results": items}

    def create_note(self, parent: str, title: str, content: str = "") -> dict:
        """Name-based replacement for the old create_page. `parent` can be
        a title ("Projects") or an id -- resolved automatically either way."""
        ref = self.resolve_name_to_id(parent)
        if not ref["success"]:
            return ref
        parent_is_db = ref["object"] == "database"
        parent_obj = {"database_id": ref["id"]} if parent_is_db else {"page_id": ref["id"]}
        title_prop = "Name" if parent_is_db else "title"

        payload = {
            "parent": parent_obj,
            "properties": {title_prop: {"title": [{"type": "text", "text": {"content": title}}]}},
        }
        if content:
            payload["children"] = [{
                "object": "block", "type": "paragraph",
                "paragraph": {"rich_text": [{"type": "text", "text": {"content": content}}]},
            }]

        result = self._request("POST", "pages", json_body=payload)
        if not result["success"]:
            return result
        data = result["data"]
        self._cache[title] = {"id": data["id"], "object": "page"}
        return {"success": True, "id": data["id"], "url": data.get("url"), "title": title}

    def read_note(self, page: str, max_blocks: int = 100) -> dict:
        ref = self.resolve_name_to_id(page)
        if not ref["success"]:
            return ref
        result = self._request("GET", f"blocks/{ref['id']}/children", params={"page_size": max_blocks})
        if not result["success"]:
            return result
        blocks = result["data"].get("results", [])
        lines = [t for t in (self._plain_text_of_block(b) for b in blocks) if t]
        return {"success": True, "id": ref["id"], "text": "\n".join(lines), "block_count": len(blocks)}

    def update_note(self, page: str, properties: dict) -> dict:
        """Update one or more property values. `properties` is Notion's
        property-value shape, e.g. {"Status": {"select": {"name": "Done"}}}."""
        ref = self.resolve_name_to_id(page)
        if not ref["success"]:
            return ref
        result = self._request("PATCH", f"pages/{ref['id']}", json_body={"properties": properties})
        if not result["success"]:
            return result
        return {"success": True, "id": ref["id"]}

    def append_to_note(self, page: str, content: str) -> dict:
        ref = self.resolve_name_to_id(page)
        if not ref["success"]:
            return ref
        payload = {"children": [{
            "object": "block", "type": "paragraph",
            "paragraph": {"rich_text": [{"type": "text", "text": {"content": content}}]},
        }]}
        result = self._request("PATCH", f"blocks/{ref['id']}/children", json_body=payload)
        if not result["success"]:
            return result
        return {"success": True, "id": ref["id"], "appended_blocks": len(result["data"].get("results", []))}

    def archive_page(self, page: str, restore: bool = False) -> dict:
        """Send a page to the trash, or restore it. Uses `in_trash`, the
        only field the 2026-03-11 API version accepts for this (the older
        `archived` field was retired in that version)."""
        ref = self.resolve_name_to_id(page)
        if not ref["success"]:
            return ref
        result = self._request("PATCH", f"pages/{ref['id']}", json_body={"in_trash": not restore})
        if not result["success"]:
            return result
        return {"success": True, "id": ref["id"], "in_trash": not restore}

    def move_page(self, page: str, new_parent: str) -> dict:
        """Reparent a page under a different page or database via the
        dedicated POST /pages/{id}/move endpoint."""
        page_ref = self.resolve_name_to_id(page)
        if not page_ref["success"]:
            return page_ref
        parent_ref = self.resolve_name_to_id(new_parent)
        if not parent_ref["success"]:
            return parent_ref
        parent_key = "data_source_id" if parent_ref["object"] == "database" else "page_id"
        payload = {"parent": {parent_key: parent_ref["id"]}}
        result = self._request("POST", f"pages/{page_ref['id']}/move", json_body=payload)
        if not result["success"]:
            return result
        return {"success": True, "id": page_ref["id"], "new_parent_id": parent_ref["id"]}

    def duplicate_page(self, page: str, new_title: Optional[str] = None) -> dict:
        """Notion's API has no native 'duplicate' endpoint. This reads the
        source page + its top-level blocks and creates a new page with the
        same parent and content (nested children beyond one level aren't
        copied -- extend this if you need deep duplication)."""
        ref = self.resolve_name_to_id(page)
        if not ref["success"]:
            return ref
        page_result = self._request("GET", f"pages/{ref['id']}")
        if not page_result["success"]:
            return page_result
        source = page_result["data"]
        title = new_title or f"{self._extract_title(source)} (copy)"

        blocks_result = self._request("GET", f"blocks/{ref['id']}/children", params={"page_size": 100})
        if not blocks_result["success"]:
            return blocks_result
        children = blocks_result["data"].get("results", [])
        clean_children = [
            {"object": "block", "type": b.get("type"), b.get("type"): b.get(b.get("type"), {})}
            for b in children
        ]

        payload = {
            "parent": source.get("parent", {}),
            "properties": {"title": {"title": [{"type": "text", "text": {"content": title}}]}},
        }
        if clean_children:
            payload["children"] = clean_children[:100]
        result = self._request("POST", "pages", json_body=payload)
        if not result["success"]:
            return result
        return {"success": True, "id": result["data"]["id"], "url": result["data"].get("url"), "title": title}

    def add_comment(self, page: str, comment: str) -> dict:
        """Requires 'insert comment' capability enabled for this integration
        in the Notion developer portal -- off by default."""
        ref = self.resolve_name_to_id(page)
        if not ref["success"]:
            return ref
        payload = {"parent": {"page_id": ref["id"]}, "rich_text": [{"text": {"content": comment}}]}
        result = self._request("POST", "comments", json_body=payload)
        if not result["success"]:
            return result
        return {"success": True, "comment_id": result["data"].get("id")}

    def upload_file(self, page: str, file_path: Optional[str] = None,
                     external_url: Optional[str] = None) -> dict:
        """Attach a file to a page's content. Either upload local bytes at
        `file_path` (single-part, <20MB) or point at an already-public
        `external_url` (skips the separate send step)."""
        ref = self.resolve_name_to_id(page)
        if not ref["success"]:
            return ref

        if external_url:
            create = self._request("POST", "file_uploads", json_body={
                "mode": "external_url", "external_url": external_url,
            })
            if not create["success"]:
                return create
            file_upload_id = create["data"]["id"]
        elif file_path:
            create = self._request("POST", "file_uploads", json_body={})
            if not create["success"]:
                return create
            file_upload_id = create["data"]["id"]
            with open(file_path, "rb") as f:
                send = self._request("POST", f"file_uploads/{file_upload_id}/send", files={"file": f})
            if not send["success"]:
                return send
        else:
            return {"success": False, "error": "Provide either file_path or external_url."}

        payload = {"children": [{
            "object": "block", "type": "file",
            "file": {"type": "file_upload", "file_upload": {"id": file_upload_id}},
        }]}
        result = self._request("PATCH", f"blocks/{ref['id']}/children", json_body=payload)
        if not result["success"]:
            return result
        return {"success": True, "page_id": ref["id"], "file_upload_id": file_upload_id}

    def create_database(self, parent_page: str, title: str, properties: Optional[dict] = None) -> dict:
        ref = self.resolve_name_to_id(parent_page)
        if not ref["success"]:
            return ref
        properties = properties or {
            "Name": {"title": {}},
            "Date": {"date": {}},
            "Status": {"select": {"options": [
                {"name": "Not started"}, {"name": "In progress"}, {"name": "Done"},
            ]}},
            "Notes": {"rich_text": {}},
        }
        payload = {
            "parent": {"type": "page_id", "page_id": ref["id"]},
            "title": [{"type": "text", "text": {"content": title}}],
            "properties": properties,
        }
        result = self._request("POST", "databases", json_body=payload)
        if not result["success"]:
            return result
        data = result["data"]
        self._cache[title] = {"id": data["id"], "object": "database"}
        return {"success": True, "id": data["id"], "url": data.get("url"), "title": title}

    def get_database_schema(self, database: str) -> dict:
        ref = self.resolve_name_to_id(database)
        if not ref["success"]:
            return ref
        result = self._request("GET", f"databases/{ref['id']}")
        if not result["success"]:
            return result
        props = result["data"].get("properties", {})
        return {"success": True, "id": ref["id"], "schema": {name: p.get("type") for name, p in props.items()}}

    def query_database(self, database: str, filter_json: Optional[dict] = None, page_size: int = 20) -> dict:
        ref = self.resolve_name_to_id(database)
        if not ref["success"]:
            return ref
        payload = {"page_size": page_size}
        if filter_json:
            payload["filter"] = filter_json
        result = self._request("POST", f"databases/{ref['id']}/query", json_body=payload)
        if not result["success"]:
            return result
        rows = [
            {"id": r["id"], "title": self._extract_title(r), "url": r.get("url"), "properties": r.get("properties")}
            for r in result["data"].get("results", [])
        ]
        return {"success": True, "results": rows}

    # ------------------------------------------------------------------
    # higher-level, task-shaped helpers Jugnu can call directly
    # ------------------------------------------------------------------

    def create_task(self, tasks_database: str, name: str, due: Optional[str] = None,
                     status: str = "Not started") -> dict:
        ref = self.resolve_name_to_id(tasks_database)
        if not ref["success"]:
            return ref
        schema = self.get_database_schema(tasks_database)
        props = {"Name": {"title": [{"type": "text", "text": {"content": name}}]}}
        if schema["success"]:
            field_types = schema["schema"]
            if status and field_types.get("Status") == "status":
                props["Status"] = {"status": {"name": status}}
            elif status and field_types.get("Status") == "select":
                props["Status"] = {"select": {"name": status}}
            if due and "Due" in field_types:
                props["Due"] = {"date": {"start": due}}
        result = self._request("POST", "pages", json_body={"parent": {"database_id": ref["id"]}, "properties": props})
        if not result["success"]:
            return result
        return {"success": True, "id": result["data"]["id"], "url": result["data"].get("url")}

    def complete_task(self, task: str) -> dict:
        """Marks a task page 'Done', auto-detecting whether its database
        uses a status- or select-type Status property. Assumes the property
        is literally named "Status" -- adjust if yours differs."""
        ref = self.resolve_name_to_id(task)
        if not ref["success"]:
            return ref
        page_result = self._request("GET", f"pages/{ref['id']}")
        if not page_result["success"]:
            return page_result
        status_prop = page_result["data"].get("properties", {}).get("Status")
        if not status_prop:
            return {"success": False, "error": "This page has no 'Status' property to update."}
        value = {"status": {"name": "Done"}} if status_prop.get("type") == "status" else {"select": {"name": "Done"}}
        return self.update_note(ref["id"], {"Status": value})

    def get_today_journal(self, journal_database: str) -> dict:
        """Assumes a journal database with a `Date` property. Returns
        today's entry if one exists; does not create one, since daily
        entries often start from a template you don't want duplicated."""
        today = date.today().isoformat()
        result = self.query_database(journal_database, filter_json={"property": "Date", "date": {"equals": today}})
        if not result["success"]:
            return result
        if not result["results"]:
            return {"success": False, "error": f"No journal entry found for {today}."}
        return {"success": True, "entry": result["results"][0]}

    def add_meeting_notes(self, meetings_parent: str, meeting_title: str, notes: str) -> dict:
        return self.create_note(parent=meetings_parent, title=meeting_title, content=notes)

    def find_project(self, name: str) -> dict:
        return self.resolve_name_to_id(name)

    def remember(self, memory_database: str, title: str, content: str) -> dict:
        """Semantically-named wrapper over create_note, for storing
        arbitrary things Jugnu is told to remember."""
        return self.create_note(parent=memory_database, title=title, content=content)

    def retrieve(self, query: str, limit: int = 5) -> dict:
        return self.semantic_search(query, limit=limit)

    # ------------------------------------------------------------------
    # (4) "semantic" search
    # ------------------------------------------------------------------

    def semantic_search(self, query: str, limit: int = 5) -> dict:
        """
        Best-effort natural-language search across cached titles, ranked by
        keyword overlap and string similarity, with a live /search fallback.

        Read this before relying on it: this is NOT true semantic search.
        It will not reliably find a page titled "Tactics Notes" from a query
        like "the Liverpool press we discussed" unless those words (or close
        variants) actually appear in the title -- there's no understanding
        of meaning here, just token overlap. Real semantic search needs an
        embeddings index: embed each page's text with an embeddings model,
        store the vectors, and rank by cosine similarity against the
        embedded query, refreshed out of band as pages change. That's a
        separate service (with its own storage and refresh job), not
        something a single synchronous method can do cheaply on every call.
        Treat this as a stand-in until that's built, not a replacement for it.
        """
        query_words = {w.lower() for w in query.split() if len(w) > 2}
        if not self._cache:
            self.sync_workspace()

        scored = []
        for title, entry in self._cache.items():
            title_words = {w.lower() for w in title.split()}
            overlap = len(query_words & title_words)
            ratio = difflib.SequenceMatcher(None, query.lower(), title.lower()).ratio()
            score = overlap * 2 + ratio
            if score > 0.3:
                scored.append((score, title, entry))
        scored.sort(key=lambda t: t[0], reverse=True)
        top = scored[:limit]

        if not top:
            live = self.search(query)
            if live["success"] and live["results"]:
                return {"success": True, "results": live["results"][:limit], "method": "live_search_fallback"}
            return {"success": False, "error": f"Nothing found matching '{query}'."}

        results = [{"title": t, "id": e["id"], "object": e["object"], "score": round(s, 2)} for s, t, e in top]
        return {"success": True, "results": results, "method": "keyword_overlap"}
