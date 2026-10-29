#!/usr/bin/env python3
"""Post the agent review's verdict to its PR, then fail the check on "block".

Run by .github/workflows/agent-review.yml after claude-code-action. The agent
answers only through its JSON verdict (``REVIEW_JSON``) and has no write access.
This script, from main, turns that verdict into the PR comment. Keeping the write
here means a prompt-injected agent can change what the comment says, but it
cannot do anything else with the token.

One comment per PR, found by a hidden marker and edited in place, so a PR with
twenty pushes has one review comment, not twenty.

Env:  REVIEW_JSON  the action's structured_output (empty if the agent produced none)
      PR, REPO, SHA, GH_TOKEN
Exit: 0 on pass, 1 on block or on no verdict at all. A silent pass on a crashed
      agent is the failure this check exists to prevent.
"""
import json
import os
import subprocess
import sys

MARKER = "<!-- ankusdrive-agent-review -->"
BOT = "github-actions[bot]"


def defang(text):
    # The agent's text is shaped by PR content: stop it from @-mentioning people.
    return str(text).replace("@", "@​")


def render(review, sha):
    verdict = review["verdict"]
    head = "**Agent review: blocking issues**" if verdict == "block" else "**Agent review: pass**"
    lines = [MARKER, f"{head} · `{sha[:7]}`", "", defang(review["summary"]).strip()]
    findings = review.get("findings") or []
    for severity, title in (("blocking", "Blocking"), ("suggestion", "Suggestions")):
        group = [f for f in findings if f.get("severity") == severity]
        if not group:
            continue
        lines += ["", f"### {title}", ""]
        for f in group:
            where = f"`{defang(f['file'])}" + (f":{f['line']}`" if f.get("line") else "`")
            lines.append(f"- {where} — **{defang(f['title'])}**  ")
            lines.append(f"  {defang(f['detail']).strip()}")
    lines += ["", "<sub>Automated review: it can block a PR but cannot approve one. "
              "A maintainer decides the merge.</sub>"]
    return "\n".join(lines)


def gh(*args, payload=None):
    out = subprocess.run(["gh", "api", *args], input=payload, capture_output=True,
                         text=True, check=True)
    return out.stdout


def main():
    repo, pr, sha = os.environ["REPO"], os.environ["PR"], os.environ["SHA"]
    raw = os.environ.get("REVIEW_JSON", "").strip()
    if not raw:
        print("::error::The review agent returned no verdict (crashed, hit its turn limit, "
              "or produced invalid output). See the Review step's log.")
        return 1
    review = json.loads(raw)
    body = render(review, sha)

    comments = json.loads(gh("--paginate", "--slurp", f"repos/{repo}/issues/{pr}/comments"))
    mine = [c for page in comments for c in page
            if c["user"]["login"] == BOT and c["body"].startswith(MARKER)]
    payload = json.dumps({"body": body})
    if mine:
        gh("-X", "PATCH", f"repos/{repo}/issues/comments/{mine[-1]['id']}", "--input", "-",
           payload=payload)
    else:
        gh("-X", "POST", f"repos/{repo}/issues/{pr}/comments", "--input", "-", payload=payload)

    if review["verdict"] == "block":
        n = sum(1 for f in review.get("findings") or [] if f.get("severity") == "blocking")
        print(f"::error::Agent review blocked this PR ({n} blocking finding(s)); see the PR comment.")
        return 1
    print("::notice::Agent review passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
