"""HTTP helper shared by the explorer and RPC clients.

Three rules:
- Network trouble (timeout, dropped connection, 429, 5xx) is retried a few times.
- A problem with the request itself (404, other 4xx) is not retried: it would fail again.
- Every request draws from one time budget, so a user never waits more than that in total.
Any failure becomes a CollectorError that says whether trying again later could help.
"""
import time

import httpx

USER_AGENT = "anychain/0.1 (+read-only EVM assistant)"
RETRY_STATUSES = {429, 500, 502, 503, 504}
RETRIES = 2  # 3 attempts in total


class CollectorError(Exception):
    """A data source failed.

    `retryable` is True for network trouble (worth trying again later)
    and False when the request itself is the problem.
    """

    def __init__(self, message: str, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


class NotFoundError(CollectorError):
    """The source answered 404: the thing does not exist there."""

    def __init__(self, message: str):
        super().__init__(message, retryable=False)


class Budget:
    """A shared deadline for all requests of one explanation.

    A soft limit: each attempt's timeout is capped by what is left, so the total
    can overshoot only by one slow read that keeps trickling data.
    """

    def __init__(self, seconds: float):
        self.deadline = time.monotonic() + seconds
        self.seconds = seconds

    def remaining(self) -> float:
        return max(0.0, self.deadline - time.monotonic())


def make_client(timeout_s: float) -> httpx.Client:
    return httpx.Client(timeout=timeout_s, headers={"User-Agent": USER_AGENT}, follow_redirects=True)


def request_json(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    params: dict | None = None,
    body: dict | None = None,
    timeout_s: float = 15,
    budget: Budget | None = None,
) -> object:
    """Send one request with retries and return the parsed JSON."""
    last_error = "no attempt made"
    for attempt in range(RETRIES + 1):
        timeout = timeout_s
        if budget is not None:
            if budget.remaining() <= 0:
                raise CollectorError(f"time budget of {budget.seconds:.0f}s used up before {url} ({last_error})")
            timeout = min(timeout_s, budget.remaining())
        try:
            resp = client.request(method, url, params=params, json=body, timeout=timeout)
        except httpx.HTTPError as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            _pause(attempt, budget)
            continue
        if resp.status_code == 404:
            raise NotFoundError(f"404 not found: {url}")
        if resp.status_code in RETRY_STATUSES:
            last_error = f"HTTP {resp.status_code}"
            _pause(attempt, budget)
            continue
        if resp.status_code >= 400:
            raise CollectorError(f"HTTP {resp.status_code} from {url}: {resp.text[:200]}", retryable=False)
        try:
            return resp.json()
        except ValueError:
            # A 200 with HTML (e.g. a bot-protection page) is usually temporary.
            last_error = "response was not JSON"
            _pause(attempt, budget)
    raise CollectorError(f"{url} failed after {RETRIES + 1} attempts: {last_error}")


def _pause(attempt: int, budget: Budget | None) -> None:
    """Short, growing wait between attempts (0.5s, 1s), never past the budget."""
    if attempt >= RETRIES:
        return
    wait = 0.5 * (attempt + 1)
    if budget is not None:
        wait = min(wait, budget.remaining())
    time.sleep(wait)
