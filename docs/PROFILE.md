# Your profile

A profile is one folder under `~/.resume-tailor/profiles/<id>/`. The Settings page edits every file in it, and
you can edit them in any text editor too; the next resume picks up the change. The demo profile
(`resume_tailor/examples/alex-rivera/`) is a complete example to copy.

```
profiles/alex-rivera/
  profile.toml        name, years, labels for the base resumes
  facts.md            the fact bank
  known_skills.txt    skills you can claim that the base resume doesn't list
  rules.toml          what the checks enforce
  resumes/
    Alex_Rivera_ML_Engineer.docx
```

## facts.md: the fact bank

This is the file that keeps the tailoring honest. Claude may only use facts from here and from your base
resume, and the honesty check enforces the part a machine can check: every number in the tailored summary and
bullets must appear in the base resume or in this file. If a bullet says "cut latency 40%" and 40 is nowhere in
either, the resume fails the check and goes back for repair.

So write down every metric you might ever want, once, with its context:

```markdown
## Lumen Freight (Mar 2023 to present), Senior Machine Learning Engineer
- Demand forecasting service (PyTorch, Airflow, AWS) for 1,200 routes; empty truck miles down 14%.
- Retrieval tool for dispatchers over 40,000 documents; 600 question eval set; accuracy 71% to 89%.
```

Writing notes go here too (tone, words you never use). The whole file is sent to Claude with every request.

When you use "Fix with note" and the note states a number or a skill ("I know dbt", "it cut cost 30%"), that is
saved to `~/.resume-tailor/confirmed/<id>.skills.txt` / `.facts.txt` and counts as yours from then on.

## known_skills.txt

One skill per line. A skill on a tailored resume that is neither in a base resume nor in this list is reported
as "not in the base resumes or known skills" and removed. Names are matched through the keyword lexicon, so
`Postgres` and `PostgreSQL` are the same skill. Add your own terms to the lexicon in `~/.resume-tailor/lexicon.json`:

```json
{"My tools": {"Quarkus": ["quarkus framework"]}}
```

## rules.toml

Every key is optional.

| Key | Example | What it does |
| --- | --- | --- |
| `bullet_words` | `[20, 32]` | a bullet outside this word range fails |
| `bullet_words_target` | `[24, 29]` | the range the prompt asks for |
| `summary_words`, `summary_sentences` | `[45, 75]`, `[3, 4]` | the same for the summary |
| `min_bullet_lines` | `2` | a bullet that renders on one line fails (most resumes look better with full lines) |
| `headline_from_job` | `true` | the title under your name follows the posting: AI Engineer, Machine Learning Engineer, Data Scientist, Data Engineer or AI/ML Engineer, picked by the posting title (see `headline.py`) |
| `headline` | `"Data Engineer"` | or one fixed title for every job |
| `summary_years` | `"6 years"` | the summary must open "<title> with 6 years" |
| `summary_must_contain` | `["6 years"]` | phrases the summary must keep |
| `era_jobs` | `["Northwind"]` | roles from before LLMs were a thing: their bullets may not mention LLMs, RAG, agents or vector databases |
| `jd_skills_allowed` | `false` | `true` lets hard skills from the job description in even if you don't list them; they are reported as "Added from JD" so you can check before applying |
| `location` | `"Portland, OR"` | the city on your contact line; a hybrid or on-site job swaps in the job's city |
| `banned_words` | `["rockstar"]` | added to the built-in list (spearheaded, leveraged, seamless, robust, ...) |
| `[verbs]` | `"Acme" = ["Built", "Ran"]` | the opening verbs one role's bullets must use, each once |

### Stricter rules for one master resume

These are for a single base resume you want kept close to how you wrote it. All are off by default.

| Key | Example | What it does |
| --- | --- | --- |
| `headline_titles` | `["AI Engineer", "Machine Learning Engineer"]` | with `headline_from_job`, only these titles; a posting whose title is outside the list gets the first one |
| `summary_keep` | `[2, 3]` | summary sentences 2 to 3 stay your base's word for word; only the first adapts to the job |
| `verbs_unique` | `true` | every bullet opens with its own verb, across the whole resume |
| `short_bullets` | `[27, 4]` | at least 4 bullets have 27 words or fewer, so the lengths vary |
| `max_at_top` | `5` | at most 5 bullets sit at exactly the top of `bullet_words` |
| `bullet_plain` | `true` | no semicolons, em or en dashes, or parentheses inside a bullet (hyphen ranges like 7-10 are fine) |
| `max_from_to` | `4` | at most 4 "from X to Y" phrasings across all bullets |
| `fact_bank_numbers` | `true` | numbers only from this base and the part of `facts.md` above a `## Part 2` heading; put interview-only details under Part 2 |
| `jd_gaps` | `true` | job skills you don't have are listed as Gaps under the review (never added); "I've used it" starts a note that confirms one |
| `skill_rows_fixed` | `true` | skill row names, order, items and rendered line counts stay your base's; Claude only reorders items inside a row |
| `hm_review` | `true` | one extra Claude call per resume reads it as the hiring manager and leaves a short report; it changes nothing |
| `mirror_duties` | `true` | Claude lists the job's 3 to 5 core responsibilities and rewords the one bullet whose real work matches each, in the job's own words; a check fails a reworded bullet that says more than your fact bank |
| `[employer_domains]` | `"Lumen Freight" = ["logistics"]` | with `mirror_duties`: a job's domain noun goes on a role's bullet only when it's listed here or in that role's facts |


### Deeper tailoring

Three more rules for one master resume, all off by default. They are checked by machine like the others, and the
review panel shows what changed under "Changes vs your base resume".

| Key | Example | What it does |
| --- | --- | --- |
| `summary_structure` | `{ verb = "shipping", max_numbers = 2, min_employers = 2 }` | the summary is written fresh for each job: sentence 1 opens "<title> with <years> shipping" and the 2 or 3 kinds of work the job cares about most, then two proofs; at most 2 numbers besides the years and at least 2 employers named. Sentences 2 and 3 can't open the same way or copy a bullet. Not with `summary_keep` |
| `summary_industries` | `["logistics", "healthcare"]` | the only words allowed after "across" in sentence 1 |
| `skills_add_first` | `true` | with `skill_rows_fixed`: every job skill you have goes into its row, job skills first in the row. A row that then overflows loses a skill only from its `[drop_order]`, first out first, and never a locked skill or one the job names |
| `locked_skills` | `["PyTorch", "SQL"]` | with `skills_add_first`: never removed |
| `[drop_order]` | `"Machine Learning" = ["Time Series", "pandas"]` | with `skills_add_first`: per skills row, what goes first when the row overflows |
| `spare_swaps` | `true` | a spare bullet from `facts.md` Part 3 (below) may take the place of a bullet in its own job when it covers the job clearly better |
| `anchor_bullets` | `["Lumen Freight forecasting"]` | with `spare_swaps`: the employer and a few words of a bullet that is never replaced |
| `max_swaps` | `2` | with `spare_swaps`: at most this many spare bullets per job (2 when not set) |

Spare bullets are true bullets you have approved but that aren't on your base resume. They go in `facts.md` under a
`## Part 3` heading, one employer per `###` heading:

```markdown
## Part 3: spare bullets

### Lumen Freight (spare bullets)
- L1 (evaluation, quality): Ran a 600 question evaluation set on every release of the dispatcher tool, lifting answer accuracy from 71% to 89%.
- L2 (variant of the drift bullet; monitoring): Paged the on-call engineer within 10 minutes when input data shifted.
```

Each line is `- <id> (<what it covers>): <the bullet>`. "variant of X" or "replaces X or Y" in the brackets means the
spare may only take the place of that bullet. With `fact_bank_numbers` a number may come from Part 3 too. A spare
can't go into another job, and its numbers can't be stated by a second bullet.

A file that doesn't parse is refused on save with the line that is wrong, and the old file stays.

## profile.toml

```toml
name = "Alex Rivera"
years = 6
file_pattern = "{name}_{title}_Resume({company}).pdf"   # the Drive / Google Doc name

[[resumes]]
file = "Alex_Rivera_ML_Engineer.docx"
id = "ml_engineer"
label = "ML Engineer"
families = ["Machine Learning Engineer", "AI Engineer"]   # break ties between bases by the posting's title
```

`claude_config_dir = "~/.claude-alex"` (above `[[resumes]]`) gives this person their own Claude Code login when
the CLI backend is used.

## More than one base resume

Put several .docx files in `resumes/` (say an ML Engineer and a Data Scientist version). For each job the app
scores every base against the job description and starts from the best one; you can override it on the New
resume form.
