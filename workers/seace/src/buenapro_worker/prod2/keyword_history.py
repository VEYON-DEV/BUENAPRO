"""Read legacy query evidence without treating accepted samples as coverage."""
from __future__ import annotations


def historical_summary(passes, observations, keywords):
    return [{"keyword": keyword,
        "historical_evidence": any(row["query"]["keyword"].casefold() == keyword.casefold() for row in passes + observations),
        "source_passes": [row for row in passes if row["query"]["keyword"].casefold() == keyword.casefold()],
        "accepted_observations": [row for row in observations if row["query"]["keyword"].casefold() == keyword.casefold()],
        "missing_evidence_means": "unknown_not_proof_never_used",
        "accepted_samples_prove_full_query_coverage": False} for keyword in keywords]


def read_history(conn, keywords):
    availability = conn.execute("""SELECT to_regclass('prod2_discovery_queries') IS NOT NULL
        AND to_regclass('prod2_discovery_runs') IS NOT NULL AS discovery,
        to_regclass('prod2_accepted_run_members') IS NOT NULL AS accepted""").fetchone()
    passes, observations = [], []
    if availability["discovery"]:
        rows = conn.execute("""SELECT q.query_manifest AS query,r.status,count(*) AS passes,
            max(r.last_observed_at) AS last_observed_at FROM prod2_discovery_queries q
            JOIN prod2_discovery_runs r USING(query_hash)
            WHERE lower(q.query_manifest->>'keyword')=ANY(%s)
            GROUP BY q.query_manifest,r.status ORDER BY q.query_manifest::text,r.status""", (keywords,)).fetchall()
        passes = [{**row, "last_observed_at": row["last_observed_at"].isoformat()} for row in rows]
    if availability["accepted"]:
        rows = conn.execute("""SELECT query_manifest AS query,count(*) AS observations,
            max(observed_at) AS last_observed_at FROM prod2_accepted_run_members
            WHERE accepted AND query_manifest IS NOT NULL
            AND lower(query_manifest->>'keyword')=ANY(%s)
            GROUP BY query_manifest ORDER BY query_manifest::text""", (keywords,)).fetchall()
        observations = [{**row, "last_observed_at": row["last_observed_at"].isoformat() if row["last_observed_at"] else None} for row in rows]
    return historical_summary(passes, observations, keywords)
