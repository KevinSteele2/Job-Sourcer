# Job Sourcer

Pulls job postings from public ATS job boards (Greenhouse, Lever, Ashby): single
companies you pick, the SimplifyJobs new-grad list, and a wide sweep of ~15,900
companies. It stores them in SQLite, drops obvious mismatches with keyword
rules, and has Claude score each remaining job 1-10 against `profile.md`, in
batches, with hard requirements (years, clearance, ...) enforced in code. You
rate the scored jobs yourself, and the places where you disagree with Claude
become rules in `feedback.md` that improve later scoring. It can run by itself
every night.

```
fetch / import / sweep  ->  score  ->  rate  ->  condense  ->  you approve feedback.md
(boards, lists)            (Claude)    (you)     (Claude)      (fed back into scoring)
```

Several ideas, and the board list in `boards/`, come from Bob's Job Search Kit:
batched and capped scoring, hard gates, the wide sweep, anchor examples, the
open-posting check, and nightly runs.

## Setup

1. **Python 3.10+** (developed on 3.13).
2. **Packages**: `pip install pyyaml flask python-docx`
3. **Claude Code CLI**, installed and logged in. Scoring runs `claude -p` as a
   subprocess and uses your Claude plan, so no API key is needed. Check it with
   `claude --version`.
4. **`profile.md`**: your background, skills, projects, and what you want.
   Scoring won't run without it.

## The app (no command line needed)

**Double-click `start.bat`.** A console window opens and the app opens in your
browser at http://127.0.0.1:5050. Close the console window to stop it. (Tip:
right-click `start.bat` -> Send to -> Desktop (create shortcut).) If it's
already running, double-clicking again just reopens the browser tab.

| Page | What you do there |
|---|---|
| **Jobs** | Browse every job by tab (Scored, To score, ★ Target companies, Filtered out, Listing only, Closed, Applied), filter by company, search titles, and sort by *Best score* or *Newest first*. Click a job for its description, Claude's reasoning, and a rating form. |
| **Rate** | Go through unrated scored jobs one at a time: pick 1-10, say why, *Save & next*, or *Skip*. |
| **Fetch** | *Import* the SimplifyJobs new-grad list, run the *Wide sweep*, *Check now* for your target companies, or fetch one company's board (pick the ATS, type the slug) and *Refresh* boards you've fetched before. *Check top jobs* confirms your best jobs are still open. Shows the last nightly run and the last 7 days of Claude usage. |
| **Score** | Pick how many this run (the cap), jobs per call, and the model, then *Start scoring*. Progress updates live; *Stop after this batch* ends a run early. *Preview prompt* shows exactly what Claude receives. |
| **Feedback** | *Propose rules* from your disagreements, edit the proposal, and save it as `feedback.md`. You can also edit `feedback.md` directly here, and *Export ratings CSV*. |
| **People** | People you know and where they work (name, company, their role, how you know them, notes). Jobs at their company show 👤 and their name in the job list and on the job's page, and each contact shows how many scored jobs their company has. Company names match on whole words, so "LCS Living" matches the board "lcs" but "Metabase" doesn't match "meta". |
| **Applications** | Jobs you've drafted for or applied to, with each one's status (applied ones first). Start a draft from a job's page with *Draft resume & cover letter*; edit it, then *Save & generate files* (PDF and Word). |

The app only listens on 127.0.0.1, so nothing outside your computer can reach it.
It calls the same functions as the command-line scripts below, so you can use
either; they share the same database.

## Command-line workflow

Everything the app does is also available as scripts.

### 1. Fetch jobs from a company's board

```
python fetch.py <greenhouse|lever|ashby> <company-slug>
python fetch.py greenhouse anthropic
```

The slug is the company's name in its board URL, e.g. `jobs.lever.co/palantir`
-> `palantir`. Each run:

- inserts jobs it hasn't seen before
- updates content and `last_seen` on jobs it has seen
- marks jobs that are no longer on the board as closed (`closed_at` is set)
- runs the keyword filter from `filters.yaml` on unscored jobs
- prints a summary: fetched, new, closed, filtered out (by reason), passed

Run it again any time to refresh. If the fetch fails (bad slug, network error),
nothing is written, so no jobs get wrongly closed.

### 1b. Import the SimplifyJobs new-grad list

```
python import_feed.py
```

Reads `listings.json` from [SimplifyJobs/New-Grad-Positions](https://github.com/SimplifyJobs/New-Grad-Positions)
(the data behind that repo's README table) and keeps active listings in the
categories set in `feeds.yaml`. For each listing:

- **Already stored**: just marked as seen; no request.
- **New, on Greenhouse, Lever, or Ashby**: the full job is fetched from that ATS,
  so it gets a description and goes through filter -> score like any other job.
- **New, anywhere else** (Workday, iCIMS, ...): stored as **listing only**
  (status `no_description`): title, company, location, and link, but no
  description, so it's never sent to Claude. Find these under the *Listing only* tab.

Jobs from the list are closed when the list stops including them. This is
separate from board fetches, so importing never closes jobs from boards you
fetched, and vice versa. A job found both ways is stored once.

The first import makes about 600 small requests with a 0.3 s pause between
each (about 5 minutes). Later imports only fetch new listings (seconds).
Listings whose posting is already gone are counted as "taken down" and skipped.

### 1c. Wide sweep of ~15,900 companies

```
python sweep.py
```

Checks every public board listed in `boards/` (Greenhouse, Lever, Ashby; from
the Job Search Kit) and keeps postings that are new, were posted within
`lookback_days`, have a title containing one of the sweep's `title_keywords`,
and pass `filters.yaml` (all in `feeds.yaml` -> `sweep`). Keep those keywords
targeted: the sweep sees every job at every company.

Plain titles with no level ("Software Engineer", "Data Engineer", listed in
`broad_title_keywords`) are where many entry-level jobs hide, but they're also
how mid and senior jobs are posted. So a plain-title match is kept only when
its description asks for at most `broad_max_years` (default 2) years of
experience, or names no number. Seniority words and foreign locations are
checked first, so Greenhouse descriptions are only fetched for plausible ones.

The keywords include `intern` and `internship` (whole words, so "Internal"
and "International" titles don't match). Internships open to recent graduates
are scored like any job; ones only for current students get the enrollment
hard gate (score capped at 2, see *Hard gates* below).

- 8 boards are checked at a time (`workers`). Greenhouse's list is fetched
  without descriptions; only matching jobs cost a second request.
- Boards that return 404 are recorded in `data/dead_boards.json` and skipped
  for 30 days. About half the list is dead, so later sweeps are faster.
- Since it downloads each board's full list, it also closes stored jobs that
  are no longer on their board.

**Boards that moved.** A dead board often means the company switched ATS
(Greenhouse -> Ashby is common), not that it closed, and none of the three
redirects an old board name. `python find_moved_boards.py` tries each dead
board's name on the other two ATSs, adds any live ones to `boards/`, and
records old -> new in `data/moved_boards.json` (boards already resolved are
skipped next time). `--dry-run` reports without changing anything, `--sample
300` checks a random few, and `--variants` also tries renamed forms ("acme"
-> "acmehq", "acme-ai" -> "acme"), which costs about 4x the requests and in a
300-board test found 1 extra. Worth re-running every month or two.

### 1d. Check top jobs are still open

```
python liveness.py                # up to 40 open jobs: tracked ones, then ones scored or rated 7+
python liveness.py --limit 100 --min-score 6
```

Asks each job's ATS about the job directly. Jobs you've drafted or marked
applied/interviewing are checked first, whatever their score, so you hear about
a closed posting before you spend time on it; then the rest, least recently
checked first. Gone -> closed. Network errors never close anything.

### 1e. Target companies (`companies.yaml`)

```
python targets.py
```

Companies you especially want (e.g. where you have a connection) are listed in
`companies.yaml`, each with an optional connection note. Every night (first
step), and from the Fetch page's *Check now*, **every** open posting at them is
pulled, whatever its title or date. Each company is read the easiest way that
works:

- **Greenhouse / Lever / Ashby** board (Codazen): the usual board API.
- **Laserfiche** (`sources/laserfiche.py`): its own jobs page, read from the HTML.
- **Oracle career sites** like LCS (`sources/oracle.py`): the public JSON API
  the site itself loads jobs from.
- **Meta** (`sources/meta.py`): Meta refuses scripts, so pages load in headless
  Edge (`sources/browser.py`), and only the searches in `companies.yaml` are
  run. Each job's details come from its schema.org `JobPosting` data.

Detail pages are fetched only for titles that pass `filters.yaml` and aren't
stored yet, so LCS's 800+ community jobs cost a few requests, and a repeat run
takes about half a minute. If a company's list comes back empty while it had
open jobs, nothing is saved (so a page that failed to load can't close them).

Target jobs (also ones the Simplify list or sweep found) get a ★, their own
*Target companies* tab (jobs that passed the filter; filtered ones are on the
*Filtered out* tab), +10 scoring priority so they're scored first, and a line
in the nightly notification when new ones appear. To add a company, tell
Claude its name: it finds which system the company's jobs are on.

### 2. Score jobs with Claude

```
python score.py --dry-run                     # print the full prompt for one job, no Claude call
python score.py --limit 50 --model sonnet     # score up to 50 jobs, 10 per call
python score.py --limit 50 --batch-size 20 --model haiku
```

| Flag | What it does |
|---|---|
| `--dry-run` | Prints the prompt for the newest unscored job and exits. |
| `--limit N` | Scores at most N jobs this run, most promising first (see *Scoring order* below). This is the cap that protects your plan. |
| `--batch-size N` | Jobs per Claude call (default 10). Your profile and rubric are sent once per batch, so bigger batches use less quota. |
| `--model NAME` | Passed to `claude --model` (`sonnet`, `haiku`, `opus`). Without it, Claude Code's default model is used. Haiku uses the least quota. |

Before scoring, every unscored job is re-checked against the current
`filters.yaml`. Each scored job gets `score`, `reason`, `matched_skills`,
`gaps`, `hard_gates`, the `model`, `scored_at`, and `prompt_version`, and its
status becomes `scored`. Descriptions over 15,000 characters are cut (the tail
is usually benefits boilerplate).

**Scoring order.** Waiting jobs are scored highest *priority* first, so a
capped run spends your plan on the most promising jobs. Priority is a cheap
points guess computed in Python before scoring, set in `filters.yaml` ->
`priority`: +3 for an entry-level title word, +1 for a target role, +1 for a
new-grad source (Simplify list or sweep), -2 for a higher-level title word,
-3 if the description asks for 3+ years. Nothing is dropped; low-priority jobs
are scored after the rest. The *To score* tab lists jobs in this order.

**Hard gates.** Claude also lists requirements you can't meet, each with an
exact quote from the posting. Python checks that the quote really is in the
posting, then caps the score: 5+ years required -> 3, 3-4 years -> 5;
clearance, required credential, people management, a non-English language, or
a non-US location -> 4; a role only for current students or later
graduates ("Class of 2027", "currently pursuing a degree") -> 2. "Preferred" or "nice to have" never counts,
and neither do ranges that start at 2 years or fewer ("0-3 years"), years with an
alternative ("3+ years or a Master's"), or years that count internships. The caps
live only in code: the rubric tells Claude to score the fit as if the gates weren't there. A quote
that isn't in the posting is shown but not applied. `fit_score` keeps Claude's
score before the cap, and the job page shows which gate capped it.

**Duplicate postings.** Some companies post one job once per city (one posting
showed up 49 times). After every filter pass, jobs with the same company, title
and description (exactly, ignoring spacing) are grouped: only the first copy is
scored and listed, with "+N more" next to its location, and its score is copied
to the others. Copies whose descriptions differ at all stay separate, because
a small difference can matter (one Palantir copy required a clearance, another
didn't). If the listed copy closes, another copy takes its place, keeping the score.

**Re-scoring when the prompt or your profile changes.** Every score is stamped
with `PROMPT_VERSION` plus a fingerprint of the rubric, instructions,
`profile.md` and `feedback.md` (e.g. `v5+292d533f`). Edit any of them and the
fingerprint changes, so jobs scored 6+ under the old one get re-checked: up to
a third of the run's limit (20 a night, set in `nightly.yaml`), counted inside
the limit. The run's output shows each one as `re-check 7 -> 8`.

**Claude usage.** Every `claude -p` call (scoring, drafting, research...) is
logged to the `claude_calls` table with its tokens, time, and API-equivalent
cost; the Fetch page shows the last 7 days by step. On a Claude plan the cost
isn't billed; it shows how much of your plan each step uses. (Scoring runs at
Claude Code's default effort: a test of `--effort low` was about 25% cheaper
but wrongly treated "willing to obtain a clearance" as a hard gate.)

If a job's entry in a batch is bad or missing, it's retried once (with the
other failures), then logged to `data/score_errors.log` and left `to_score`
for the next run. If Claude Code reports a usage limit, the run stops
immediately (exit code 2); everything already scored is saved.

**Posting dates and order.** Each job stores when the company posted it
(`posted_at`, from each source's own date; Laserfiche gives none, so those
show when the job was first found, marked "found"). The Jobs page shows the
age as a badge ("today", "3d", "2w"; green for the last 3 days). Lists are
sorted best score first and, among equal scores, newest first; *Newest first*
sorts by date alone. Jobs you mark applied leave every list except the
*Applied* tab and the Applications page.

**Older than 14 days.** Postings more than 14 days old (by posting date, or
when first found if the source gives none) leave the Scored, To score and
Listing only tabs for their own *Older than 14 days* tab. They're kept, with
their scores and ratings, not deleted, but they aren't scored, re-checked or
offered for rating, so no Claude usage goes to them. Jobs at your target
companies stay put whatever their age. Change the age with `MAX_AGE_DAYS` in
`db.py`.

### 3. Rate the scored jobs yourself

```
python rate.py              # rate unrated jobs
python rate.py --limit 10   # stop after 10
python rate.py --export     # write all ratings to data/ratings.csv
```

You see each job's title, location, link, Claude's score and reason, matched
skills, and gaps. Then:

| Key | Action |
|---|---|
| `1`-`10` | Your rating. Then type why (or press Enter to skip). |
| `d` | Show the full description. |
| `o` | Open the posting in your browser. |
| `s` | Skip this job (it comes back next session). |
| `q` | Quit. Ratings already entered are saved. |

Jobs come highest-score first, with every 4th job taken from the lowest scores,
so you catch good jobs Claude under-rated, not only over-rated ones.

**The "why" matters most when you disagree with the score by 2 or more.** That
comment is what `condense.py` learns from. Rating a job again replaces your
earlier rating.

### 4. Turn your ratings into rules

```
python condense.py                 # ratings that differ from Claude by 2+
python condense.py --threshold 3   # only bigger disagreements
```

Sends Claude your current `feedback.md`, the rubric, and every commented rating
that differs from the model's score by the threshold or more, and asks for an
updated rule list (at most 15 general rules). The result goes to
**`feedback.proposed.md`**. It never edits `feedback.md` itself.

Read the proposal, then copy the rules you agree with into `feedback.md`. Ratings
without a comment are skipped, since there's no reason to learn from.

### 5. Repeat

`feedback.md` is included in every scoring prompt, so newly scored jobs use your
rules right away.

## Resume and cover letter drafts

From a job's page, click **Draft resume & cover letter** (or run
`python drafter.py <job id>`). In a few minutes you get a one-page resume and a
cover letter tailored to that job, written from `full-profile.md`:

- **Requirements check first.** If scoring found a requirement you can't meet
  (5+ years, a clearance, current students only...) and its quote was verified
  in the posting, *Draft* first shows those requirements in the posting's own
  words. *I've read this, draft anyway* continues with everything you entered;
  *Don't draft* goes back. Jobs without such requirements draft straight away.
- **Skill check first** (on by default; the *Ask me about skill gaps first*
  box). One quick Sonnet call (about 10 seconds) lists what the posting asks
  for that `full-profile.md` doesn't show, each with the posting's exact
  words. Python drops any whose quote isn't really in the posting, any the
  profile already names, and any you've answered before. You answer each
  Yes (with a line on where and how) or No, or skip it. **Yes** answers are
  added to `full-profile.md` under *Skills -> Confirmed while drafting*, in
  your words, so this draft and every later one can use them. **No** answers
  are remembered (`skill_answers` table) and never asked again. If nothing is
  missing, drafting starts straight away.

0. **Company research** (Claude Sonnet with web search, `research.py`): the
   company's stated mission and values, culture, products, recent news, and
   early-career programs, each with a source link and the company's exact
   words. Python downloads each source page and checks the quote is on it;
   facts whose quote isn't there are dropped, and pages that block scripts
   are marked *unchecked*. Cached per company for 30 days. If it fails, the
   draft is written without it. This is the only Claude call allowed any
   tools, and only these two (pre-approved with `--allowedTools`).
1. **Tailor** (Claude Opus): picks the most relevant projects and experience,
   rewrites the bullets for this job, and writes the cover letter following the
   profile's *Writing voice* and *Stories* sections. Each item records which part
   of the profile it came from. The letter plan quotes the posting line or
   researched company value each paragraph answers. Facts from your "Why this
   company?" notes that your profile lacks are listed under *Add to your
   profile?* (the profile itself is never edited).

   **Letter shape** (your rules from Oct 23, 2026, in `full-profile.md` ->
   *Letter rules from the outside review*): 3 paragraphs, 250-350 words. (1)
   The role and your strongest evidence for it; (2) one story told as what you
   checked, found and changed (bug and verification stories are welcome when
   relevant); (3) one specific tie to the company, then relocation for on-site
   roles outside Southern California, and stop. Requirements are shown through
   what you did, never echoed back ("the language your posting asks for"). The
   drafter also sees the first and last paragraphs of your 10 most recent
   letters (`<recent_letters>`) and must not reuse their openings, closings or
   story framings.
2. **Truth check** (a second, separate Opus call): compares every claim with
   `full-profile.md` and fixes anything invented, overstated, marked
   `[confirm]`, or pointing out a gap. Its fixes and suggestions are listed at
   the top of the editor. It also returns its **evidence**: for each resume
   bullet, the summary and each letter paragraph, the exact words in the
   profile (or your notes, the posting, the research) that back it. Python
   then checks each quote really is in that source, the same way hard-gate
   quotes are checked, so the check doesn't rest on the model's word. Resume
   facts must be quoted from the profile or your notes, never the posting. A
   quote that isn't found, or a line with no quote, becomes a warning.
3. **Code checks**: skills not found in the profile are removed (whole terms,
   so "R" can't match inside another word); banned phrases ("I am writing to
   apply", stock closers like "excited for the opportunity", "leveraged"...),
   em dashes, more than 3 colons, a letter outside 250-350 words or not 3
   paragraphs, and any 8-word phrase repeated from another of your letters
   are flagged.

**Resume layout.** Header, a one-sentence summary, Education, Projects (the main
section: the 2 most relevant get 2-3 bullets, a tools line and their repo link;
two more get one bullet), Experience, then Skills as labeled lines. The education
line, experience headings, project names and links, and the "Additional
experience" line (retail and lifeguarding) come from the **Canonical headings**
section of `full-profile.md` and print exactly as written there; a draft or an
edit that rewords one is put back, or flagged if it can't be matched. Drafts
made before Oct 23, 2026 open and render in this layout automatically.

Then edit any line in the app and click **Save & generate files**. The PDFs use
the same layout and font (Cambria) as your current resume, printed by Microsoft
Edge, and land in `applications/<Company - Role>/` (the HTML and draft JSON are
in its `source/` folder). If the resume runs past one page, you're told so.
Word (`.docx`) copies of both are written next to them (`docx_export.py`, with
python-docx), for sites that want Word or parse it better: same text, order
and font, at the font size that fit the PDF on one page. They're plain
paragraphs and real Word bullets (no tables, text boxes or headers), which
resume parsers read most reliably. Word lays text out itself, so open the
resume once to check it's still one page.
The resume header shows your location (with "open to relocation"), phone and
email, and Education (with your GPA from `full-profile.md`) comes right after
the Summary, since a recruiter looks for a recent graduate's degree first.
Two postings with the same company and title share one folder, so drafting
the second overwrites the first one's files.

**Feedback and revision.** On a draft, *Get feedback on the letter* has Claude
review the current cover letter (about a minute): whether each story fits
this company, accuracy, voice compared with your own letter, then flow. Issues
are listed by severity with quotes and suggestions. The high and medium ones
are prefilled into a box: edit them, add your own notes, and *Revise the
letter* rewrites only the cover letter (truth-checked again; the resume is
untouched). The letterhead can be switched off per draft with a checkbox.

**Resume only** is ticked by default (letters matter most at small companies,
startups, mission-driven orgs and referrals). For applications that don't take a cover letter, keep
*Resume only* on the Draft (or Redraft) form. The draft skips the company
research and the letter, so it's quicker; the truth check covers only the
resume, and *Save & generate files* makes just the resume (removing cover
letter files left from an earlier full draft of the same job). Redraft without
the box ticked to get a letter later. From the command line:
`python drafter.py <job id> --resume-only`.

`full-profile.md` is the only source of facts. The drafter edits it in one
place only: your Yes answers from the skill check. Keep it up to date: new
facts there show up in the next draft.

**Versions.** Every draft keeps its history in the `draft_versions` table:
*generated* (as the drafter wrote it), *revised* (after a letter revision),
*rendered* (each *Save & generate*), and *sent*. When you first mark a job
applied, the current PDF and Word files are copied into a `sent <date>` folder
next to them, so a later redraft can't overwrite what the company got. The
draft page's *Versions* panel lists them and shows what you changed by hand
from the generated draft, which is the best guide to what the prompts should
do differently.

## Tracking applications

Every job page (and draft page) has an **Application status** panel. Click
**Mark as applied** when you submit, then update it as things move:
Applied -> Interviewing -> Offer or Rejected, each with an optional note
("phone screen 10/12"). The panel shows the full history, and *Undo / clear
history* removes it if you clicked by mistake.

**Reached out for a referral.** If you know someone at the company, ask them
for a referral first, then click **Reached out for a referral** (the note box
is for who you asked). The job leaves the working lists like an applied one,
but `applied_at` isn't set, and the panel shows the date to apply by. If you
still haven't marked it applied 2 days later (`REMINDER_DAYS` in `db.py`), you
get a Windows notification (`remind.py`, run hourly by the **Job Sourcer
Reminders** task, once per reach-out), a red banner on every page of the app
until you mark it applied, and a line in the nightly notification. Mark it
applied when you apply, whether through the referral or directly.
Turn the hourly task off with `Disable-ScheduledTask -TaskName "Job Sourcer
Reminders"`.

Applied jobs get a status pill everywhere they're listed, their own **Applied**
tab on the Jobs page (most recent first), and a place at the top of the
Applications page. The status is separate from scoring and from closing: a job
stays marked applied even after the posting comes down.

## Nightly runs

A Windows Task Scheduler task, **Job Sourcer Nightly**, runs `nightly.py` every
day at 2:00 AM: check target companies -> import the Simplify list -> wide
sweep -> check top jobs are open -> score up to 60 jobs (new ones, plus up to
20 re-checks). Turn steps on or off and set the scoring cap, re-check share and
model in `nightly.yaml`. When it finishes, a Windows notification says how it
went: new jobs at your target companies, new jobs, how many scored 7+, Claude
usage, and any step that failed (`notify.py`; try it with
`python notify.py`).

- It wakes the computer if it's asleep, and if 2 AM is missed (the computer
  was off), it runs as soon as it can. You need to be logged in.
- It runs with `pythonw`, so no window appears. Output goes to
  `data/logs/nightly-<date>.log`, and the Fetch page shows the last run's result.
- A lock file stops two nightly runs overlapping, and the app won't start a
  scoring run while the nightly one is going (both would score the same jobs).
- It keeps the PC awake until it finishes (Windows' `SetThreadExecutionState`),
  so the machine can't fall asleep halfway through a long run.
- Quiet failures are called out in the notification:
  - An expired Claude login stops scoring at once and the notification says
    "Claude login expired: run `claude /login`" instead of looking like a quiet night.
  - Scoring shows how many failed and how many are still waiting.
  - If the sweep reaches less than half its usual boards, or the Simplify list
    shrinks by half, or new jobs drop below a quarter of the usual (the median of
    the last 7 runs), it warns "low volume".
- Run it now: Task Scheduler -> Job Sourcer Nightly -> Run, or `python nightly.py`.
- Turn it off: Task Scheduler -> Job Sourcer Nightly -> Disable, or in
  PowerShell `Disable-ScheduledTask -TaskName "Job Sourcer Nightly"`.
  Remove it with `Unregister-ScheduledTask -TaskName "Job Sourcer Nightly"`.

## Files you edit

| File | Purpose |
|---|---|
| `full-profile.md` | Your complete background, writing voice and stories: the only source of facts for resumes and cover letters. Mark anything unverified `[confirm]` and drafts won't use it. |
| `profile.md` | Your background. Included word-for-word in every scoring prompt. Its **Anchor examples** section (what a 10, 8, 6 and 4 look like for you) calibrates every score. |
| `feedback.md` | Scoring rules learned from your ratings. Optional; created by you from `feedback.proposed.md`. |
| `filters.yaml` | Keyword rules for the cheap pre-filter (see below). |
| `feeds.yaml` | Which SimplifyJobs categories to import, and the wide sweep's title keywords, lookback, and speed. |
| `companies.yaml` | Your target companies: how each one's jobs are read, plus an optional `connection` note shown on its jobs. |
| `nightly.yaml` | Which steps the nightly run does, and its scoring cap and model. |
| `scorer.py` -> `RUBRIC`, `INSTRUCTIONS` | The scoring rubric and instructions. Changing them (or `profile.md`/`feedback.md`) triggers re-checks automatically; bump `PROMPT_VERSION` for a big change so it's easy to read which version scored what. |

### `filters.yaml`

```yaml
title:
  include: [engineer, developer, data, analyst]   # pass only if the title has one of these (empty = allow all)
  exclude: [senior, staff, principal, sales]      # any match = filtered out (checked first)
location:
  include: []                                     # pass only if ANY location matches (empty = allow all)
  exclude: [uk, london, india, canada]            # filtered out only if EVERY location matches
```

- Matching is case-insensitive and whole-word: `data` matches "Data Engineer"
  but not "Metadata", and `engineer` does **not** match "Engineering" (list both).
- Blank locations always pass. The filter is meant to drop only obvious mismatches.
- A place that's clearly in the US is never excluded, even if a word in it is on the
  exclude list: "United States", "USA" or "US", or a US state after a comma ("Vienna, VA",
  "Dublin, Ohio", "Berlin, CT"), unless it also names another country.
- Edits take effect before the next scoring run: every unscored job is
  re-checked and its priority recomputed. Jobs that are already scored are never touched.
- The `priority` section only sets scoring order; it never filters anything out.

## Code layout

| File | What it does |
|---|---|
| `start.bat` | Double-click launcher for the app. |
| `app.py` | The Flask web app: one function per page. Scoring runs in a background thread that the Score page polls for progress. |
| `templates/` | The pages' HTML (Jinja templates). `base.html` is the shared layout and nav. |
| `static/style.css` | All styling. Follows your system's light/dark mode. |
| `fetch.py` | `run_fetch()`: fetch one board -> save to the database -> filter -> summary. Also a command. |
| `import_feed.py` | `run_import()`: import the SimplifyJobs list (see 1b). Also a command. |
| `sweep.py` | `run_sweep()`: the wide sweep (see 1c). Also a command. |
| `boards/*.json` | The ~15,900 board names the sweep checks (from the Job Search Kit, plus moved boards found by `find_moved_boards.py`). |
| `find_moved_boards.py` | Finds the new ATS of boards that went dead (see 1c). |
| `liveness.py` | `check_open()`: confirm top jobs are still open (see 1d). Also a command. |
| `nightly.py` | The nightly run: every step above in order, logged. |
| `targets.py`, `companies.yaml` | `run_targets()`: check every posting at your target companies (see 1e). Also a command. |
| `sources/laserfiche.py`, `oracle.py`, `meta.py`, `browser.py` | Readers for target companies that aren't on the three ATS boards. |
| `drafter.py` | `make_draft()` (research -> tailor -> truth check -> code checks) and `render()` (HTML -> PDF with Edge, plus Word files). Also a command. |
| `docx_export.py` | The resume and cover letter as Word files, built with python-docx. |
| `research.py` | `get_research()`: web research on a job's company, quotes checked against their pages, cached per company. Also a command: `python research.py <job id>`. |
| `remind.py` | Windows "time to apply" notification for jobs you reached out about 2+ days ago (run hourly by Task Scheduler). |
| `notify.py` | `notify(title, message)`: a Windows notification, through PowerShell. |
| `templates/documents/` | The resume and cover letter page layouts. |
| `sources/simplify.py` | Reads Simplify's `listings.json` and works out which ATS, board, and job ID each posting link points to. |
| `sources/greenhouse.py`, `lever.py`, `ashby.py` | One adapter per ATS. Each has `fetch_jobs(slug) -> list[Job]` and handles that ATS's quirks (e.g. Lever splits descriptions across three fields). |
| `sources/__init__.py` | `ADAPTERS`: maps `greenhouse`/`lever`/`ashby` to its adapter. |
| `sources/http.py` | Shared `get_json(url)` helper. |
| `job.py` | The `Job` dataclass every adapter returns, plus `html_to_text`. |
| `filters.py` | Loads `filters.yaml` and checks a job's title and location. |
| `db.py` | SQLite schema, migrations, and every query. |
| `scorer.py` | Builds the batch prompt, calls `claude -p`, validates each job's verdict, checks gate quotes and applies the caps. |
| `score.py` | `score_jobs()` and `preview_prompt()`: score `to_score` jobs. Also a command. |
| `rate.py` | Command: rate scored jobs in the terminal, or export ratings to CSV. |
| `condense.py` | `propose_rules()`: propose `feedback.md` rules from your disagreements. Also a command. |

To add another ATS: write `sources/<name>.py` with a `fetch_jobs(slug)` that
returns `Job`s, and add it to `ADAPTERS`.

## Database (`data/jobs.db`)

Open it with any SQLite viewer (e.g. the "SQLite Viewer" VS Code extension or
DB Browser for SQLite).

### `jobs` table

| Column | Meaning |
|---|---|
| `id` | Row number. |
| `source`, `company`, `ats_job_id` | Which ATS, which board, and the ATS's own job ID. Unique together. Listing-only jobs use `simplify`, the company name, and Simplify's ID. |
| `feed` | `simplify` (job list), `sweep` (wide sweep), or `NULL` (board fetch). |
| `title`, `location`, `url`, `description` | The posting. The description is converted to plain text. |
| `first_seen` / `last_seen` | First and most recent run that saw the job on its board (UTC). |
| `closed_at` | When it disappeared from its board. `NULL` = still open. |
| `status` | `to_score`, `filtered_out`, `scored`, or `no_description` (listing only; can't be scored). |
| `filter_reason` | Why it was filtered out, e.g. `title excludes "sales"`. |
| `score`, `reason`, `matched_skills`, `gaps` | Claude's verdict. The two lists are JSON arrays. `score` is after hard-gate caps. |
| `fit_score`, `hard_gates` | Claude's score before caps, and the gates it found: JSON `[{gate, quote, verified}]`. |
| `checked_at` | Last time `liveness.py` confirmed the posting was open. |
| `app_status`, `applied_at` | Where your application stands (`applied`, `interviewing`, `offer`, `rejected`) and when you first marked it applied. History is in the `application_events` table. |
| `priority` | Scoring order for waiting jobs: higher is scored first. |
| `model`, `scored_at`, `prompt_version` | Which model scored it, when, and the prompt stamp (`PROMPT_VERSION` + fingerprint of the prompt and profile). |
| `posted_at` | When the company posted it (UTC), when the source says. |
| `duplicate_of` | For a duplicate copy, the id of the copy that's scored and listed. |
| `target` | 1 if the company is in `companies.yaml`. |

Closing is separate from status: a job you've scored stays `scored` even after
the posting comes down, and if the posting reappears, `closed_at` is cleared.

### `ratings` table

| Column | Meaning |
|---|---|
| `job_id` | The job you rated (one rating per job). |
| `my_rating`, `comment` | Your 1-10 rating and why. |
| `model_score`, `prompt_version` | Claude's score at the moment you rated, kept so a later re-score doesn't change what you disagreed with. |
| `rated_at` | When you rated it (UTC). |

### Useful queries

```sql
-- Best open jobs
SELECT score, company, title, url FROM jobs
WHERE status = 'scored' AND closed_at IS NULL ORDER BY score DESC;

-- What the filter removed, by reason
SELECT filter_reason, COUNT(*) FROM jobs
WHERE status = 'filtered_out' GROUP BY filter_reason ORDER BY 2 DESC;
```

## Troubleshooting

- **`profile.md is missing`**: write it; `score.py` needs it. (`--dry-run` works without it.)
- **`` `claude` not found on PATH ``**: install Claude Code, or open a new terminal so PATH updates.
- **Jobs failing to score**: check `data/score_errors.log` for the error and raw reply.
- **Stopped early, usage limit**: wait for your plan's limit to reset, then rerun `score.py`; it continues where it stopped.
- **Too many or too few jobs filtered**: look at `filter_reason` counts in `fetch.py`'s summary and adjust `filters.yaml`.
- **Nightly run didn't happen**: check Task Scheduler -> Job Sourcer Nightly -> History, and `data/logs/`. A leftover `data/nightly.lock` from a crash is ignored after 3 hours.

## Not built yet

- Phone notifications for the nightly run (it notifies on this PC only).
- Web UI extras: editing `profile.md` and `filters.yaml` from the app.
