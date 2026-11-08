-- propose_note as an MCP URL-mode elicitation (docs/per-action-consent.md).
--
-- A client that supports URL elicitation opens the approval page itself and,
-- once the person is done, retries the original tool call. The retry must not
-- propose the same note again, and after approval it must not post it twice.
-- So the server first asks for this person's latest proposal of the same text on
-- the same task in the last hour: still waiting, the same link comes back;
-- approved or denied, the retry reports that and proposes nothing.

create or replace function counsel.proposal_for_text(p_auth_user uuid, p_task_title text, p_body text)
returns table (proposal_id uuid, status text, expires_at timestamptz)
language sql stable security definer set search_path = '' as $$
  select p.id,
         case when p.decision is not null then p.decision
              when p.expires_at <= now() then 'expired'
              else 'waiting' end,
         p.expires_at
  from counsel.proposals p
  join counsel.logins l on l.person_id = p.person_id and l.auth_user_id = p_auth_user
  join work.tasks t on t.id = p.task_id and t.title = p_task_title
  where p.body = p_body and p.created_at > now() - interval '1 hour'
  order by p.created_at desc
  limit 1
$$;

revoke all on function counsel.proposal_for_text(uuid, text, text) from public;
grant execute on function counsel.proposal_for_text(uuid, text, text) to counsel_mcp;
