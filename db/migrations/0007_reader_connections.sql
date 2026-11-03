-- Grafana Cloud runs a dashboard's panels from several query servers, each
-- with its own pool, so one page load opened more than 3 connections and
-- random panels failed with "too many connections for role". 10 covers a
-- full dashboard load and is still a small share of the database's
-- connections; the agents use counsel_runtime and are never queued behind it.
alter role counsel_reader connection limit 10;
