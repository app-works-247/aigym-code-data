"""Minimal GitHub REST client (standard library only).

Handles what the nightly job needs: token auth, rate limits (primary and secondary),
and retries on server errors. The HTTP layer is injectable so tests never touch the network.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass

API_ROOT = "https://api.github.com"
USER_AGENT = "aigym-code-data"
SEARCH_PAGE_SIZE = 100
SEARCH_RESULT_CAP = 1000  # GitHub never returns more than this for one search query
MAX_FILE_BYTES = 200_000  # dependency files are small; never hold a huge file in memory
NETWORK_ERROR_STATUS = 599  # not a real HTTP status: the request never got an answer

_MAX_RETRIES = 4
_MAX_SLEEP_SECONDS = 900


@dataclass(frozen=True)
class Response:
    status: int
    headers: Mapping[str, str]
    body: bytes


Transport = Callable[[str, Mapping[str, str]], Response]


class GitHubError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"GitHub API {status}: {message}")
        self.status = status


def urllib_transport(url: str, headers: Mapping[str, str], body: bytes | None = None) -> Response:
    """GET, or POST when `body` is given (GraphQL)."""
    request = urllib.request.Request(url, data=body, headers=dict(headers))
    try:
        with urllib.request.urlopen(request, timeout=30) as resp:
            return Response(
                resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
            )
    except urllib.error.HTTPError as err:
        return Response(err.code, {k.lower(): v for k, v in err.headers.items()}, err.read())
    except (urllib.error.URLError, TimeoutError, ConnectionError) as err:
        # Timeouts and dropped connections are retried like a server error.
        return Response(NETWORK_ERROR_STATUS, {}, str(err).encode())


class GitHubClient:
    def __init__(
        self,
        token: str | None = None,
        transport: Transport = urllib_transport,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": USER_AGENT,
        }
        if token:
            self._headers["Authorization"] = f"Bearer {token}"
        self._transport = transport
        self._sleep = sleep
        self._now = now
        self.request_count = 0

    def search_repositories(self, query: str, page: int = 1) -> dict:
        """One page of /search/repositories, most stars first (so a capped query keeps the top)."""
        params = {"q": query, "per_page": SEARCH_PAGE_SIZE, "page": page, "sort": "stars"}
        return self.get("/search/repositories", params)

    def tree(self, owner: str, name: str, ref: str) -> dict:
        """Every path in the repo at `ref` (one request). `truncated` is true for huge repos."""
        path = f"/repos/{_q(owner)}/{_q(name)}/git/trees/{_q(ref)}"
        return self.get(path, {"recursive": 1})

    def file_text(self, owner: str, name: str, path: str, ref: str) -> str:
        """Raw text of one file, cut to MAX_FILE_BYTES."""
        url_path = f"/repos/{_q(owner)}/{_q(name)}/contents/{_q(path)}"
        body = self._request(url_path, {"ref": ref}, accept="application/vnd.github.raw+json")
        return body[:MAX_FILE_BYTES].decode("utf-8", "replace")

    def get(self, path: str, params: Mapping[str, object] | None = None) -> dict:
        return json.loads(self._request(path, params))

    def graphql(
        self, query: str, variables: Mapping[str, object], retries: int = _MAX_RETRIES
    ) -> dict:
        """Run one GraphQL query and return `data`.

        GitHub answers 200 even when part of a query fails. Nodes that could not be found come
        back as null and are left to the caller; any other error fails the call.
        """
        body = json.dumps({"query": query, "variables": dict(variables)}).encode()
        reply = json.loads(self._request("/graphql", body=body, retries=retries))
        errors = [e for e in reply.get("errors") or [] if e.get("type") != "NOT_FOUND"]
        if errors or reply.get("data") is None:
            message = "; ".join(str(e.get("message")) for e in errors) or "no data"
            # GitHub reports its own query timeout as a 200 with an error message.
            status = 504 if "timeout" in message.lower() else 200
            raise GitHubError(status, f"GraphQL: {message[:300]}")
        return reply["data"]

    def _request(
        self,
        path: str,
        params: Mapping[str, object] | None = None,
        accept: str | None = None,
        body: bytes | None = None,
        retries: int = _MAX_RETRIES,
    ) -> bytes:
        url = API_ROOT + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = dict(self._headers, Accept=accept) if accept else self._headers
        for attempt in range(retries + 1):
            if body is None:
                resp = self._transport(url, headers)
            else:
                resp = self._transport(
                    url, dict(headers, **{"Content-Type": "application/json"}), body
                )
            self.request_count += 1
            if resp.status == 200:
                self._respect_exhausted_limit(resp.headers)
                return resp.body
            wait = self._retry_delay(resp, attempt)
            if wait is None or attempt == retries:
                raise GitHubError(resp.status, _error_message(resp))
            self._sleep(wait)
        raise AssertionError("unreachable")

    def _respect_exhausted_limit(self, headers: Mapping[str, str]) -> None:
        # Wait now instead of burning the next request on a guaranteed 403.
        if headers.get("x-ratelimit-remaining") == "0":
            self._sleep(self._until_reset(headers))

    def _until_reset(self, headers: Mapping[str, str]) -> float:
        reset = headers.get("x-ratelimit-reset")
        wait = float(reset) - self._now() + 1 if reset else 60.0
        if wait > _MAX_SLEEP_SECONDS:
            raise GitHubError(403, f"rate limit resets in {int(wait)}s, too long to wait")
        return max(wait, 1.0)

    def _retry_delay(self, resp: Response, attempt: int) -> float | None:
        if resp.status in (403, 429):
            if "retry-after" in resp.headers:  # secondary rate limit
                return min(float(resp.headers["retry-after"]), _MAX_SLEEP_SECONDS)
            if resp.headers.get("x-ratelimit-remaining") == "0":
                return self._until_reset(resp.headers)
            return None  # a real permission problem: retrying will not help
        if resp.status >= 500:
            return float(2 ** (attempt + 1))
        return None


def _q(part: str) -> str:
    """Quote one URL path piece; keeps '/' so file paths and branch names like a/b still work."""
    return urllib.parse.quote(part, safe="/")


def _error_message(resp: Response) -> str:
    try:
        return str(json.loads(resp.body).get("message", ""))[:300]
    except (ValueError, AttributeError):
        return resp.body[:300].decode("utf-8", "replace")
