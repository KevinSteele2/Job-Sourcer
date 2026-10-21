"""Turn your ratings into proposed scoring rules for feedback.md.

Usage:
    python condense.py                 use ratings that differ from the model by 2+
    python condense.py --threshold 3   only bigger disagreements
    python condense.py --model sonnet

Writes feedback.proposed.md. It never touches feedback.md: read the proposal,
then copy the rules you agree with into feedback.md yourself.
"""

import argparse
import re
import sys
from pathlib import Path

import db
import scorer

PROPOSED_PATH = Path(__file__).parent / "feedback.proposed.md"

INSTRUCTIONS = """\
You maintain a short list of scoring rules for an AI job-fit scorer. The scorer
already follows the rubric below; these rules are added to its prompt to correct
where its scores differ from the candidate's own judgment.

Below are the current rules, then jobs where the candidate's rating differed
from the scorer's score, each with the candidate's explanation.

Write the complete updated rule list:
- Keep current rules that still hold. Merge or sharpen rules the new ratings refine.
  Drop a rule only if the ratings clearly contradict it.
- Each rule is one line, general (not about a single company or posting), and
  says which way to move the score and roughly how much.
- Only add rules the candidate's explanations support. Don't invent preferences.
- Don't repeat what the rubric already says.
- At most 15 rules.

Respond with ONLY a markdown bullet list (lines starting with "- "), no heading
and no other text."""


def build_prompt(current_rules: str, rows: list[tuple]) -> str:
    ratings = "\n\n".join(
        f"Job: {title} ({company}, {location})\n"
        f"Scorer gave {model_score}: {reason}\n"
        f"Candidate gave {my_rating}: {comment}"
        for company, title, location, model_score, reason, my_rating, comment in rows)
    return f"""{INSTRUCTIONS}

<rubric>
{scorer.RUBRIC}
</rubric>

<current_rules>
{current_rules or "(none yet)"}
</current_rules>

<ratings>
{ratings}
</ratings>"""


def propose_rules(threshold: int, model: str | None) -> dict:
    """Ask Claude for an updated rule list. Used by this command and the web app.

    Returns {"rules": [...], "used": n, "no_comment": n, "model": name}.
    "rules" is empty when there's nothing to condense. Raises
    scorer.UsageLimitError / scorer.ScoringError if the Claude call fails.
    """
    conn = db.connect()
    rows = db.disagreements(conn, threshold)
    no_comment = conn.execute("""
        SELECT COUNT(*) FROM ratings
        WHERE ABS(my_rating - model_score) >= ? AND comment IS NULL
    """, (threshold,)).fetchone()[0]
    conn.close()

    info = {"rules": [], "used": len(rows), "no_comment": no_comment, "model": None}
    if not rows:
        return info

    current = scorer.FEEDBACK_PATH.read_text(encoding="utf-8").strip() if scorer.FEEDBACK_PATH.exists() else ""
    text, info["model"] = scorer.call_claude(build_prompt(current, rows), model, stage="condense")

    # Keep only the bullet lines, in case the model added a preamble or fences.
    info["rules"] = [line for line in text.splitlines() if re.match(r"\s*[-*] ", line)]
    if not info["rules"]:
        raise scorer.ScoringError(f"reply had no bullet list: {text[:300]}")
    return info


def main() -> None:
    parser = argparse.ArgumentParser(description="Propose feedback rules from your ratings.")
    parser.add_argument("--threshold", type=int, default=2,
                        help="minimum |your rating - model score| to include (default 2)")
    parser.add_argument("--model", help="model alias or name passed to claude --model")
    args = parser.parse_args()

    print("Condensing...")
    try:
        info = propose_rules(args.threshold, args.model)
    except scorer.UsageLimitError as e:
        sys.exit(f"Usage limit reached, try later: {e}")
    except scorer.ScoringError as e:
        sys.exit(f"Claude call failed: {e}")

    if info["no_comment"]:
        print(f"Skipped {info['no_comment']} disagreement(s) with no comment (no reason to learn from).")
    if not info["rules"]:
        print(f"No commented ratings differ from the model by {args.threshold}+. Nothing to condense.")
        return

    PROPOSED_PATH.write_text("\n".join(info["rules"]) + "\n", encoding="utf-8")
    print(f"Used {info['used']} rating(s). Wrote {len(info['rules'])} proposed rule(s) "
          f"to {PROPOSED_PATH.name} ({info['model']}).")
    print("Review it, then copy the rules you agree with into feedback.md.")


if __name__ == "__main__":
    main()
