-- counsel_reader: the login dashboards use (Grafana). It reads the cost views
-- and nothing else: dollars, tokens and run metadata, never a prompt, a
-- ticket, a task or a secret. The views run as their owner, so no underlying
-- table is granted. `counsel audit` fails if the reader can reach anything
-- more. The password is set out of band, never in the repo:
--   alter role counsel_reader password '...';

do $$
begin
  if not exists (select from pg_roles where rolname = 'counsel_reader') then
    create role counsel_reader login noinherit;
  end if;
end $$;

-- No NOSUPERUSER or NOREPLICATION: on Supabase the admin is not a superuser and may not
-- name those attributes, even to turn them off. A new role has neither; the audit checks.
alter role counsel_reader login noinherit nocreatedb nocreaterole nobypassrls
  connection limit 3;
-- A slow dashboard query must never hold up the agents.
alter role counsel_reader set statement_timeout = '10s';
alter role counsel_reader set default_transaction_read_only = on;

grant usage on schema counsel to counsel_reader;
grant select on counsel.call_costs, counsel.run_costs, counsel.model_prices to counsel_reader;
