# HTTP API

Everything the web page does goes through this API, so another program can do it too. I use it from my job
tracker: when I mark a job as "want to apply", the tracker posts the job description here and later shows the
finished resume's link next to the job.

The server listens on `http://127.0.0.1:8770` (settings `port`). It answers only this machine unless
`allow_lan` is set in `~/.resume-tailor/settings.json`.

## Auth

Every request needs the access token from `~/.resume-tailor/token` (Settings > Job tracker shows it too):

```
Authorization: Bearer <token>
```

Opening `/?token=<token>` once in a browser sets an HttpOnly cookie instead. `python -m resume_tailor token`
prints that link.

## Queue a resume

`POST /api/jobs`

```json
{
  "profile": "alex-rivera",
  "company": "Acme Logistics",
  "role": "Senior Machine Learning Engineer",
  "jd_text": "the whole job description ...",
  "jd_url": "https://example.com/jobs/123",
  "tracker_job_id": "your-own-id-42",
  "base_override": null,
  "note": null,
  "workplace": "hybrid",
  "location": "Seattle, WA"
}
```

Only `profile` and `jd_text` (80 characters or more) are required. Company and role are guessed from the text
when missing. `workplace` is `remote`, `hybrid` or `onsite`; for a hybrid or on-site job, `location` ("City, ST")
replaces the city on the contact line. A location that isn't "City, ST" is never refused: the app reads the city
out of it, or the job waits as `needs_place` until someone picks one. Without `workplace`, the app reads both
from the job description.

Send `{"jobs": [ ... ]}` to queue several at once (all or nothing).

Answer: `201 {"ok": true, "ids": [17], "worker_awake": true}`.

## Read results

| Request | What you get |
| --- | --- |
| `GET /api/jobs?profile=<id>` | every job of that person with its result |
| `GET /api/jobs?profile=<id>&lean=1` | the same list with only `base`, `approved` and `dropped` in each result (small, for a tracker's list view) |
| `GET /api/jobs/<id>` | one job, its result and its job description |
| `GET /api/jobs/<id>/file/pdf` | the tailored PDF (redirects to a URL ending in the file name). Also `docx`, `png` (page image), `base.pdf`, `base.png`. Add `?dl=1` to stamp it as downloaded |
| `GET /api/state?profile=<id>` | what the page shows: profiles, jobs, archived jobs, settings, worker status |
| `GET /api/events?since=<event id>` | server-sent events: status changes, notes, toasts. The page refreshes on them. `since=latest` sends only what happens from now on |


A job's `status` is one of `queued`, `generating`, `checking`, `ready`, `needs_you` (it failed checks and waits
for you to accept or fix it), `needs_place`, `waiting` (Claude isn't reachable yet), `failed` or `cancelled`.

The useful parts of `result`:

- `files`: paths of `pdf`, `docx`, `png`, `base_pdf`
- `checks`: `{ok, failures: [{check, name, message}], info}`; `info.ats` has `base_pct`, `tailored_pct`, `missing`
- `change_notes`, `review`, `gaps`, `added_from_jd`: what Claude said it changed and why
- `skill_gaps`: job skills the person doesn't have, listed instead of added (rules.toml `jd_gaps`)
- `hm_review`: `{reads_human, clear_fit, score, notes, at, model}`, the hiring-manager report (`hm_review`)
- `mirror`: `{responsibilities, domain, mirrored: [{responsibility, bullet_id, phrase_used, at}], unmatched, covered,
  total}`, the job's responsibilities and which bullets now carry them (`mirror_duties`); null without the rule

- `diff`: bullet by bullet, base against tailored
- `attempts`: every round with its kind (full, repair, mechanical, fix, reimport), failures, model, tokens, cost
- `approved`: `{file_id, link, name, at}` once it is uploaded to Drive

## Act on a job

`POST /api/jobs/<id>/<action>`

| Action | Body | What it does |
| --- | --- | --- |
| `approve` | | uploads the PDF to Google Drive (`<drive_root>/<first name>/`); never overwrites another file |
| `gdoc` | | makes an editable Google Doc copy |
| `reimport` | | reads your edits back from that Doc, refills the base layout and runs the checks again (no Claude call) |
| `fix` | `{"note": "..."}` | one Claude call that changes only what the note names |
| `accept` | | moves a `needs_you` resume to Ready as it is |
| `retry`, `regenerate` | `{"note"?, "base_override"?}` | starts over |
| `undo_edit` | | back to the version before the last note or re-import |
| `place` | `{"location": "City, ST" or "remote"}` | answers a `needs_place` job |
| `cancel`, `archive`, `delete`, `top` | | cancel, hide, delete for good (archived only, with an undo window), move to the front of the queue |

Batch actions: `POST /api/archive {"what": "approved" | "cancelled_failed", "profile"?}`,
`POST /api/unarchive {"ids": [...]}`, `POST /api/undelete {"ids": [...]}`,
`POST /api/archived/clear {"profile"}`.

## Helpers

- `POST /api/jd/fetch {"url"}`: the job description from a link. Greenhouse, Lever and Ashby links go through
  their public job APIs; other pages are fetched only where robots.txt allows. LinkedIn, Indeed and Glassdoor are never fetched (paste the text).
- `POST /api/jd/guess {"text"}`: `{company, role}` guessed from pasted text.
- `POST /api/settings {...}`: change `model`, `effort`, `claude_backend`, `pdf_engine`, `concurrency`,
  `max_attempts`, `max_repairs`, `max_claude_calls`, `archived_delete_days`, `tracker_url`, `default_profile`.

## Setup endpoints (used by the Settings page)

`GET /api/setup` returns the status rows. `POST /api/setup/anthropic_key {"key"}`,
`POST /api/setup/google_client {"client_id", "client_secret"}` or `{"json"}` or `{"remove": true}`,
`POST /api/setup/google/disconnect`, `POST /api/setup/test {"what": "claude" | "google" | "pdf"}`,
`GET /api/profiles/<id>/files`, `POST /api/profiles/<id>/file {"name", "text"}`,
`POST /api/profiles/<id>/resume {"filename", "data": base64}`, `POST /api/profiles {"id", "name"}`,
`POST /api/demo/install`. `GET /oauth/google/start` sends the browser to Google's consent screen and
`/oauth/google/callback` receives the answer.

## Example

```sh
TOKEN=$(cat ~/.resume-tailor/token)
curl -s -H "Authorization: Bearer $TOKEN" -X POST http://127.0.0.1:8770/api/jobs \
  -d "{\"profile\": \"alex-rivera\", \"jd_text\": $(python -c 'import json,sys; print(json.dumps(open(sys.argv[1]).read()))' job.txt)}"
curl -s -H "Authorization: Bearer $TOKEN" "http://127.0.0.1:8770/api/jobs/1" | python -m json.tool | head -40
```
