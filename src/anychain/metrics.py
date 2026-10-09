"""Usage metrics from the event log (PHASE3 T1, R6, D45): the original plan's queries, printed with their SQL
by `anychain metrics` (the README of Phase 4 shows the same SQL).

Every query reads the `runs` table of the local SQLite log (events.py) and counts answers given to people:
`explain` and the API (source cli or api), from `:since` (a Unix time), written since these metrics exist (mode
recorded). The acceptance run (source canary) and batches are left out. `answers` counts each answer;
`transactions` counts each distinct transaction once (one asked twice is two answers). Percentages are shares of
the query's answers.
"""
import re

QUERIES = [
    ("Failure causes (the diagnosis rule of each failed transaction)", """
SELECT COALESCE(rule, 'no conclusion') AS cause, COUNT(*) AS answers,
       COUNT(DISTINCT network || tx_hash) AS transactions, 100.0 * COUNT(*) / SUM(COUNT(*)) OVER () AS share
FROM runs
WHERE ts >= :since AND source IN ('cli', 'api') AND mode IS NOT NULL AND status = 'failed'
GROUP BY cause
ORDER BY answers DESC
"""),
    ("Diagnosis labels (CONFIRMED / LIKELY / UNKNOWN)", """
SELECT COALESCE(diagnosis, 'unlabelled') AS label, COUNT(*) AS answers,
       COUNT(DISTINCT network || tx_hash) AS transactions, 100.0 * COUNT(*) / SUM(COUNT(*)) OVER () AS share
FROM runs
WHERE ts >= :since AND source IN ('cli', 'api') AND mode IS NOT NULL AND status = 'failed'
GROUP BY label
ORDER BY answers DESC
"""),
    ("ABI source of the main call (explorer / repo / signature-database candidates / raw)", """
SELECT abi_source, COUNT(*) AS answers, COUNT(DISTINCT network || tx_hash) AS transactions,
       100.0 * COUNT(*) / SUM(COUNT(*)) OVER () AS share
FROM runs
WHERE ts >= :since AND source IN ('cli', 'api') AND mode IS NOT NULL AND abi_source IS NOT NULL
GROUP BY abi_source
ORDER BY answers DESC
"""),
    ("Satisfaction by mode (thumbs up and down from the page)", """
SELECT mode, COALESCE(SUM(feedback = 'up'), 0) AS up, COALESCE(SUM(feedback = 'down'), 0) AS down,
       100.0 * SUM(feedback = 'up') / NULLIF(SUM(feedback IS NOT NULL), 0) AS satisfaction
FROM runs
WHERE ts >= :since AND source IN ('cli', 'api') AND mode IS NOT NULL
GROUP BY mode
ORDER BY mode
"""),
    ("Cache (answers served without asking the explorer and the node)", """
SELECT CASE WHEN cache = 'hit' THEN 'hit' WHEN cache = 'stored' THEN 'stored'
            WHEN cache LIKE 'not kept%' THEN 'not kept' ELSE COALESCE(cache, 'unknown') END AS cache,
       COUNT(*) AS answers, 100.0 * COUNT(*) / SUM(COUNT(*)) OVER () AS share
FROM runs
WHERE ts >= :since AND source IN ('cli', 'api') AND mode IS NOT NULL
GROUP BY 1
ORDER BY answers DESC
"""),
    ("Chat questions by intent and by what decided the answer (intent 'human': a hand-off request; D74)", """
SELECT COALESCE(intent, 'unknown') AS intent, COALESCE(path, 'unknown') AS path, COUNT(*) AS questions,
       100.0 * COUNT(*) / SUM(COUNT(*)) OVER () AS share
FROM runs
WHERE ts >= :since AND source = 'chat'
GROUP BY intent, path
ORDER BY questions DESC
"""),
]


def run_queries(log, since: float) -> list[list[dict] | set[str]]:
    """Each query's rows; for a query whose columns this log does not have yet (written before they existed, and
    opened read-only), the set of missing columns instead."""
    with log._connect() as db:
        columns = {r[1] for r in db.execute("PRAGMA table_info(runs)")}
        out = []
        for _title, sql in QUERIES:
            needed = ({"rule", "diagnosis", "abi_source", "feedback", "mode", "cache", "source", "intent", "path"}
                      & set(re.findall(r"\w+", sql)))
            if needed - columns:
                out.append(needed - columns)
                continue
            out.append([dict(r) for r in db.execute(sql, {"since": since})])
        return out
