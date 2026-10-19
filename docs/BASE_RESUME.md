# The base resume layout

The app never generates a resume from scratch. It refills your own .docx: the text inside the existing runs is
replaced and every paragraph property, font, tab stop and margin stays exactly what you made. That is why the
PDF looks like your resume, and why the layout has to be one the reader understands.

Top to bottom:

1. Your name (first paragraph)
2. The title under it (second paragraph)
3. The contact line (third paragraph). If it contains the city from `rules.toml` `location`, that city is
   swapped for the job's city on hybrid and on-site jobs. Links on this line are kept.
4. Section headings, each alone on its line: `Summary`, `Skills` (or `Technical Skills`), `Experience` (or
   `Work Experience`, `Professional Experience`) and `Education`.
5. Summary: one or more paragraphs.
6. Skills: one line per category, written `Category: item, item, item` with the category in bold. Several lines
   may share one paragraph with line breaks.
7. Experience: for each role, a line `Company, Country` then a tab (or a run of spaces) then the dates
   (for example Mar 2023 to Present, written with a hyphen or an en dash); the next line is the job title; then the bullets as a bulleted
   list.
8. Education: lines with optional dates at the right.

A right-aligned tab stop for the dates is the most reliable: every PDF engine draws it the same way. If your
resume pushes dates right with typed spaces, the app measures how each engine draws it and trims spaces until no
date wraps (`basefit.py`).

The tailored resume keeps the same number of skill lines, roles and bullets as the base, and the same companies,
titles, dates and education, character for character. The checks fail anything else.

`tools/make_example_resume.py` builds the demo resume with python-docx and is a working example of the layout.

## What the eight checks look at

| # | Check | Fails when |
| --- | --- | --- |
| 1 | Page | the PDF isn't exactly one page, or the page size or margins changed |
| 2 | Layout | a section, role, title, date, education line or contact detail changed; a date wraps |
| 3 | Fonts | the .docx gained a font or size the base never had, or the PDF uses a new font |
| 4 | Line fit | a bullet has a widow (a last line under 20% of the width), renders on one line, or a skills line wraps more than the base's |
| 5 | Honesty | a skill you don't list, a number that is in neither the base nor the fact bank, or a GenAI tool in an era job |
| 6 | ATS | the keyword score fell below the base's, or a required keyword you own is missing |
| 7 | Writing | banned words, em dashes in bullets, word counts, the summary opening, the location |
| 8 | Margins | ink past the page margins, or lines overlapping |

When checks fail, cheap fixes run first with no Claude call (dropping an unknown skill, un-hyphenating a line
that won't break), then repair rounds send Claude only the failing lines with the rule each one breaks. After
`max_claude_calls` the resume waits in the Queue with the problems listed in plain words.
