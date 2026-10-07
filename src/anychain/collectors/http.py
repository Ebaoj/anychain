"""Small HTTP helper with retries. Every network failure becomes CollectorError."""
import time

import httpx

USER_AGENT = "anychain/0.1 (+read-only EVM assistant)"


class CollectorError(Exception):
    """A data source failed. The message says which one and why."""


def get_json(client: httpx.Client, url: str, params: dict | None = None, retries: int = 2) -> dict | list:
    last = "unknown error"
    for attempt in range(retries + 1):
        try:
            resp = client.get(url, params=params)
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(0.5 * (attempt + 1))
                continue
            if resp.status_code == 404:
                raise CollectorError(f"404 not found: {url}")
            resp.raise_for_status()
            return resp.json()
        except CollectorError:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            last = f"{type(exc).__name__}: {exc}"
            if attempt < retries:
                time.sleep(0.5 * (attempt + 1))
    raise CollectorError(f"request failed for {url}: {last}")


def make_client(timeout_s: float) -> httpx.Client:
    return httpx.Client(timeout=timeout_s, headers={"User-Agent": USER_AGENT}, follow_redirects=True)
