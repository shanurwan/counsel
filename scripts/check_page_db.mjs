// The demo page runs every migration in PGlite. If one stops loading there, or an
// attack the page shows stops being refused, the page is wrong, so CI fails here.
// Usage: node check_page_db.mjs <schema.sql>
import { readFileSync } from "node:fs";
import { PGlite } from "@electric-sql/pglite";

const db = await PGlite.create();
await db.exec(readFileSync(process.argv[2], "utf8"));
await db.exec("set session authorization counsel_runtime");

const REFUSED = [
  ["agent_team_digest", "update work.tasks set owner_id = null", "42501"],
  ["agent_team_digest", "update work.tasks set status = 'done'", "42501"],
  ["agent_support_drafter", "select token from support.integration_tokens", "42501"],
  ["agent_support_drafter", "insert into support.ticket_messages (ticket_id, body) select id, 'x' from support.tickets", "42501"],
  ["agent_team_digest", "delete from counsel.audit_log", "42501"],
  ["agent_team_digest", "set role postgres", "42501"],
];

let failed = 0;
for (const [role, sql, code] of REFUSED) {
  await db.exec(`begin; set local role "${role}"`);
  let got = "allowed";
  try {
    await db.exec(sql);
  } catch (e) {
    got = e.code;
  }
  await db.exec("rollback");
  if (got !== code) failed += 1;
  console.log(got === code ? "refused" : `WRONG (${got})`, role, sql);
}
process.exit(failed ? 1 : 0);
