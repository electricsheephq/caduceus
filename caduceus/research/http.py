"""Small, allowlisted JSON transport. No credentials or genotype data belong here."""
from __future__ import annotations

import json
import time
from contextlib import contextmanager
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

HOSTS = frozenset({"www.ebi.ac.uk", "eutils.ncbi.nlm.nih.gov", "www.ncbi.nlm.nih.gov", "api.cpicpgx.org"})
USER_AGENT = "caduceus-hermes-plugin/0.1 (+https://github.com/electricsheephq/caduceus)"
MAX_RESPONSE_BYTES = 8 << 20


class BudgetExceeded(RuntimeError):
    """The watch has used its physical request allowance, including retries."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Do not let urllib follow a redirect outside the checked origin.
        return None


class UrllibHttp:
    def __init__(self, *, sleep=time.sleep, monotonic=time.monotonic):
        self.sleep = sleep
        self.monotonic = monotonic
        self._last_ncbi = None
        self._remaining = None
        self.request_count = 0
        self._opener = build_opener(_NoRedirect())

    @contextmanager
    def limited(self, budget):
        """One watch's request allowance; retries consume it too. Use one client per job."""
        previous = self._remaining
        self._remaining = max(0, int(budget))
        try:
            yield
        finally:
            self._remaining = previous

    def get_json(self, url, params) -> dict:
        parts = urlsplit(url)
        if (parts.scheme != "https" or parts.hostname not in HOSTS or parts.username or parts.password
                or parts.port not in (None, 443) or parts.fragment):
            raise ValueError("URL must use HTTPS on an allowed public-science host")
        query = dict(parse_qsl(parts.query))
        query.update(params or {})
        ncbi = parts.hostname in {"eutils.ncbi.nlm.nih.gov", "www.ncbi.nlm.nih.gov"}
        if ncbi:
            query["tool"] = "caduceus"
        target = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))
        request = Request(target, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        for attempt in range(3):
            if self._remaining is not None:
                if self._remaining <= 0:
                    raise BudgetExceeded("request budget exhausted")
                self._remaining -= 1
            if ncbi and self._last_ncbi is not None:
                self.sleep(max(0.0, 1 / 3 - (self.monotonic() - self._last_ncbi)))
            if ncbi:
                self._last_ncbi = self.monotonic()
            self.request_count += 1
            try:
                with self._opener.open(request, timeout=20) as response:
                    raw = response.read(MAX_RESPONSE_BYTES + 1)
                    if len(raw) > MAX_RESPONSE_BYTES:
                        raise ValueError("JSON response exceeds the 8 MB size limit")
                    return json.loads(raw)
            except (HTTPError, URLError, TimeoutError) as exc:
                retryable = not isinstance(exc, HTTPError) or exc.code == 429 or 500 <= exc.code < 600
                if attempt == 2 or not retryable:
                    raise
                self.sleep(attempt + 1)
        raise AssertionError("unreachable")
