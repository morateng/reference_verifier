"""Shared per-client provider pacing and arXiv Atom metadata lookup."""
import asyncio
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse
from weakref import WeakKeyDictionary

import httpx


class ProviderGate:
    def __init__(self, interval: float, sleep=asyncio.sleep, clock=time.monotonic):
        self.interval, self.sleep, self.clock = interval, sleep, clock
        self.lock = asyncio.Lock()
        self.next_request = 0.0
        self.cooldown_response = None

    async def get(self, client, url, attempts=4, **kwargs):
        async with self.lock:
            response = None
            for attempt in range(attempts):
                delay = self.next_request - self.clock()
                if delay > 30 and self.cooldown_response is not None:
                    return self.cooldown_response
                if delay > 0:
                    await self.sleep(delay)
                response = await client.get(url, **kwargs)
                self.next_request = self.clock() + self.interval
                if response.status_code not in {429, 502, 503, 504}:
                    self.cooldown_response = None
                    return response
                retry = response.headers.get("retry-after", "")
                try:
                    wait = max(0, float(retry))
                except ValueError:
                    try:
                        wait = max(0, (parsedate_to_datetime(retry) - datetime.now(timezone.utc)).total_seconds())
                    except (TypeError, ValueError, OverflowError):
                        wait = 2 ** (attempt + 1)
                # Share the cooldown with other queued lookups. Never retry earlier
                # than Retry-After; a long cooldown is returned as a failed lookup.
                self.next_request = max(self.next_request, self.clock() + wait)
                self.cooldown_response = response
                if wait > 30 or attempt == attempts - 1:
                    return response
            return response


_gates = WeakKeyDictionary()


async def provider_get(client: httpx.AsyncClient, url: str, **kwargs):
    host = urlparse(url).hostname
    gates = _gates.setdefault(client, {})
    gate = gates.setdefault(host, ProviderGate(3.1 if host == "export.arxiv.org" else 1.1))
    return await gate.get(client, url, **kwargs)


def parse_arxiv(data: bytes, requested: str) -> dict | None:
    atom = "{http://www.w3.org/2005/Atom}"
    requested_base = re.sub(r"v\d+$", "", requested)
    for entry in ET.fromstring(data).findall(atom + "entry"):
        identifier = entry.findtext(atom + "id", "").split("/abs/")[-1]
        if re.sub(r"v\d+$", "", identifier) != requested_base:
            continue
        # Preserve explicit version requests. Never substitute a different version.
        if re.search(r"v\d+$", requested) and identifier != requested:
            continue
        authors = []
        for author in entry.findall(atom + "author"):
            name = author.findtext(atom + "name", "").strip()
            given, _, family = name.rpartition(" ")
            authors.append({"given": given, "family": family})
        title = " ".join(entry.findtext(atom + "title", "").split())
        year = entry.findtext(atom + "published", "")[:4]
        if not title or not year.isdigit():
            continue
        return {"title": [title], "author": authors, "published": {"date-parts": [[int(year)]]},
                "container-title": ["arXiv preprint arXiv:" + identifier],
                "abstract": " ".join(entry.findtext(atom + "summary", "").split()),
                "_source": "arXiv", "_arxiv_id": identifier,
                "_source_url": "https://arxiv.org/abs/" + identifier, "type": "preprint"}
    return None


async def arxiv_work(client: httpx.AsyncClient, identifier: str) -> dict | None:
    response = await provider_get(client, "https://export.arxiv.org/api/query", params={"id_list": identifier, "max_results": 1})
    response.raise_for_status()
    return parse_arxiv(response.content, identifier)
