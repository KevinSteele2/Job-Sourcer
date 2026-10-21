# Job Sourcer

## What this is
A personal tool that pulls job postings from public ATS job boards, filters them,
and scores each one against my background using Claude. I rate the results, and my
feedback is saved to a file that guides future scoring. This is phase 1 of a larger
job-search system; resume and cover letter agents come later.

## How I want to work
- One step at a time. Don't build ahead of the current step.
- Explain the mechanism behind what you write (why this approach, what the code is
  doing), not just working code. I want to understand and maintain this myself.
- Prefer small, targeted edits over rewriting whole files.
- Ask before adding a new dependency.

## Environment
- Windows, VS Code, Python 3
- Use pathlib for file paths so nothing breaks on Windows separators.

## Architecture (planned)
1. **Fetch**: pull postings from public endpoints, no scraping, no auth.
   - Greenhouse: https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true
   - Lever: https://api.lever.co/v0/postings/{slug}?mode=json
   - Ashby: https://api.ashbyhq.com/posting-api/job-board/{slug}
2. **Normalize**: convert all three into one common job format.
3. **Filter**: cheap rule-based pass (title keywords, seniority, location, already seen)
   before anything goes to the AI.
4. **Score**: Claude reads profile.md + feedback.md + the job description and returns
   JSON: score (1-10) and a short reason.
5. **Feedback**: I rate jobs and explain why. Raw ratings go to a CSV; feedback.md
   holds condensed rules that get included in the scoring prompt.
6. **Interface**: a clean local web UI listing jobs with score, reason, and status.
    Lets me filter/sort, rate jobs, add feedback, and mark applied. Storage is SQLite
    so the UI can query and update individual jobs.

## Key files
- `profile.md`: my background, skills, projects, and what I want in a role
- `feedback.md`: condensed scoring rules learned from my ratings
- `companies.yaml`: companies to watch, with ATS type and slug
- `data/`: fetched jobs, seen-job IDs, raw ratings (not committed)

## Rules
- Never hardcode API keys. Read them from environment variables.
- Don't edit profile.md or feedback.md unless I ask.
- Be polite to the endpoints: small delay between requests, no hammering.

## Current status
Step 4: 