"""Keep AI tools off the repo's GitHub contributors list.

GitHub credits a contributor for every `Co-authored-by:` trailer and every commit author on
the default branch. This check fails if any commit in a range credits Claude or Anthropic,
either as a co-author or as the author/committer (e.g. a `claude[bot]` commit from an agent).

    python -m nbaforecast.attribution origin/main..HEAD   # CI on a pull request
    python -m nbaforecast.attribution HEAD                # CI on main: the whole history
"""

import re
import subprocess
import sys

COAUTHOR = re.compile(r"^co-authored-by:.*(claude|anthropic)", re.IGNORECASE | re.MULTILINE)
IDENTITY = re.compile(r"claude\[bot\]|@anthropic\.com", re.IGNORECASE)

# One record per commit: hash, author, committer, then the message. \x1e separates commits.
LOG_FORMAT = "%H%x1f%an <%ae>%x1f%cn <%ce>%x1f%B%x1e"


def violations(log_output: str) -> list[str]:
    problems = []
    for record in filter(str.strip, log_output.split("\x1e")):
        sha, author, committer, message = record.strip().split("\x1f", 3)
        for match in COAUTHOR.finditer(message):
            problems.append(f"{sha[:8]}: co-author trailer {match.group(0)!r}")
        for role, identity in (("author", author), ("committer", committer)):
            if IDENTITY.search(identity):
                problems.append(f"{sha[:8]}: {role} is {identity}")
    return problems


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    rev_range = args[0] if args else "HEAD"
    log = subprocess.run(
        ["git", "log", f"--format={LOG_FORMAT}", rev_range],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    problems = violations(log)
    for problem in problems:
        print(f"attribution check failed: {problem}", file=sys.stderr)
    if problems:
        print(
            "Remove the trailer (git commit --amend) or re-author the commit before merging.",
            file=sys.stderr,
        )
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
