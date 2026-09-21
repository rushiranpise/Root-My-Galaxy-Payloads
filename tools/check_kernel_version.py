#!/usr/bin/env python3
"""Fail a build whose module version did not come from the tag.

`kernel/Kbuild` derives the module's version as `30000 + git rev-list --count HEAD` at the tag and
prints it while configuring. That is where the number comes from - it is not ours to set, and the
manager pairs itself with whatever the module reports - so nothing in this repository writes a
version down. Two ways the derivation does not happen, and neither is visible in an exit status:

* **Upstream's own fallback branch.** With no `.git` to count, Kbuild takes it: its `$(info)` line
  says `version fallback`, not `version`, and the module reports 1 (KernelSU-Next) or 16
  (KernelSU). The manager then compares itself against a number that means nothing.
* **A checkout with no history.** A depth-1 clone counts 1, so the module reports 30001 and looks
  plausible. Kbuild does try `git fetch --unshallow` when it sees a shallow clone, which is a
  network call inside the DDK container that can fail quietly.

So the number is asserted here instead, against the tree this job is building, and the expected value
is computed rather than recorded: a tag's commit count is a fact about the tag.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys

VERSION_BASE = 30000
"""Where upstream starts counting, in both flavours - their `Kbuild` says `30000 + git version`."""

# `MULTILINE`, because a build log is read whole and every line of it is a candidate: this is also
# what keeps `version fallback:` from being read as a version - it has no colon-and-number after
# the word `version`.
REPORTED = re.compile(r"-- KernelSU(?:-Next)? version: (?P<version>[0-9]+)[ \t]*\r?$", re.MULTILINE)
FALLBACK = re.compile(r"version fallback: (?P<version>[0-9]+)[ \t]*\r?$|KSU_GIT_VERSION not defined", re.MULTILINE)


def commits_at(tree: str) -> int | None:
    result = subprocess.run(
        ["git", "-C", tree, "rev-list", "--count", "HEAD"], capture_output=True, text=True, check=False
    )
    if result.returncode != 0 or not result.stdout.strip().isdigit():
        return None
    return int(result.stdout.strip())


def is_shallow(tree: str) -> bool:
    """A depth-1 clone counts 1 commit and cannot be told apart from a tag whose history is 1 long."""
    result = subprocess.run(
        ["git", "-C", tree, "rev-parse", "--is-shallow-repository"], capture_output=True, text=True, check=False
    )
    return result.stdout.strip() == "true"


def head(tree: str) -> str | None:
    result = subprocess.run(
        ["git", "-C", tree, "rev-parse", "HEAD"], capture_output=True, text=True, check=False
    )
    return result.stdout.strip() or None


def exact_tag(tree: str) -> str | None:
    """The tag that points at HEAD, if one does.

    `--exact-match` and not `--abbrev=0`: the latter answers with the *nearest* tag, which is what a
    build past the tag would be described by, and that is the case worth telling apart.
    """
    result = subprocess.run(
        ["git", "-C", tree, "describe", "--tags", "--exact-match", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() or None


def write_receipt(path: str, version: int, commit: str, ref: str, tag: str | None) -> None:
    """What this half stamped, for the half that cannot see this checkout.

    The two halves of a pair are built in different jobs from different clones, and the job that
    publishes them has neither. A receipt is how the version a module was built with travels beside
    the artifact it is stamped into, so the pair can be compared at all - and so the comparison at
    publish time does not depend on a checkout that no longer exists.
    """
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(f"version={version}\n")
        handle.write(f"commit={commit}\n")
        handle.write(f"ref={ref}\n")
        handle.write(f"tag={tag or ''}\n")
    print(f"wrote {path}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--log", required=True, help="the build's output, as Kbuild printed it")
    parser.add_argument("--tree", required=True, help="the KernelSU checkout the build used")
    parser.add_argument("--receipt", help="write the version this build stamped, for check_pair_version.py")
    parser.add_argument("--ref", help="the tag this build was asked for, recorded in the receipt")
    arguments = parser.parse_args()

    with open(arguments.log, encoding="utf-8", errors="replace") as handle:
        text = handle.read()

    expected_count = commits_at(arguments.tree)
    if expected_count is None:
        print(f"error: {arguments.tree} is not a git checkout to count commits in", file=sys.stderr)
        return 1
    if is_shallow(arguments.tree):
        # The one case the comparison below cannot see: a shallow checkout and a build from that same
        # checkout agree with each other on a number that is wrong for the tag. Check out with
        # `fetch-depth: 0` and this stops being possible.
        print(
            f"error: {arguments.tree} is a shallow clone, so its commit count is 1 and the module's\n"
            f"       version would be {VERSION_BASE + 1} rather than this tag's. Fetch the whole\n"
            "       history for the tag this pair is built from.",
            file=sys.stderr,
        )
        return 1

    expected = VERSION_BASE + expected_count
    reported = {int(match.group("version")) for match in REPORTED.finditer(text)}

    if not reported:
        print(
            "error: the build printed no KernelSU version line, so Kbuild took its fallback branch.\n"
            "       The module would report the fallback number rather than this tag's.",
            file=sys.stderr,
        )
        for line in text.splitlines():
            if FALLBACK.search(line):
                print(f"       {line.strip()[:160]}", file=sys.stderr)
        return 1

    wrong = sorted(value for value in reported if value != expected)
    if wrong:
        print(
            f"error: the module reports {', '.join(str(value) for value in wrong)}, but this checkout "
            f"is {expected_count} commits\n"
            f"       into its history, which is {expected}. A shallow clone counts 1 and reports "
            f"{VERSION_BASE + 1}, which\n"
            "       looks plausible and is wrong; see the note about --unshallow in "
            "kernel/Kbuild.",
            file=sys.stderr,
        )
        return 1

    print(f"module version {expected} = {VERSION_BASE} + {expected_count} commits at this tag")

    if arguments.receipt:
        commit = head(arguments.tree)
        if commit is None:
            print(f"error: {arguments.tree} has no HEAD to record", file=sys.stderr)
            return 1
        tag = exact_tag(arguments.tree)
        write_receipt(arguments.receipt, expected, commit, arguments.ref or tag or "", tag)
        print(f"  at commit {commit[:12]}, tag {tag or '(none exactly)'}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
