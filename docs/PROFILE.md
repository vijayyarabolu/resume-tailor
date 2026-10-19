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
