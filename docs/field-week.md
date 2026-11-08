# Field run: issue triage on real repositories

In progress since 8 Nov 2026. Counsel's `issue_triage` agent reads every new issue on
`supabase/auth`, `supabase/supabase`, `supabase/supabase-js` and `supabase/cli` in shadow mode:
it writes a triage draft inside Counsel and posts nothing to GitHub. `counsel field`
replaces this page with every draft set beside what the maintainers actually did.
