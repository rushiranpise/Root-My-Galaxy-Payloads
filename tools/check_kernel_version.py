#!/usr/bin/env python3
"""Fail a build whose module version did not come from the tag.

`kernel/Kbuild` derives the module's version as `30000 + git rev-list --count HEAD` at the tag and
prints it while configuring. That is where the number comes from - it is not ours to set, and the
manager pairs itself with whatever the module reports - so nothing in this repository writes a
version down.

The arithmetic is the project's own and differs between them: KernelSU and KernelSU-Next both count
from 30000, while ReSukiSU adds 700 to the same count (`30000 + $(KSU_LOCAL_VERSION) + 700`), which is
why a flavour is an input here rather than an assumption. Each project also names itself on the line
it prints, and that name is held against the flavour this run asked for: a build that says `KernelSU`
while the job asked for `resukisu` is a build from the wrong repository, which is the one mistake the
number alone cannot see.

Two ways the derivation does not happen, and neither is visible in an exit status:

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
"""Where every project starts counting - each one's `Kbuild` says `30000 + git version`."""

# What each flavour adds on top of that base, and the name the project prints beside its number.
#
# KernelSU and KernelSU-Next count the commits alone. ReSukiSU adds 700 - the same +700 sits in its
# `kernel/Kbuild` and in its `userspace/ksud/build.rs`, under a comment that calls it "for historical
# reasons" - so a pair built there reports 30700 + commits, and reading that with KernelSU's
# arithmetic would refuse a build that is exactly right. The offset is written down rather than
# discovered because there is nowhere to discover it from: it is a constant in someone else's build
# files, and a release that changed it would have to be read by a person.
FLAVOURS: dict[str, tuple[str, int]] = {
    "kernelsu": ("KernelSU", 0),
    "kernelsu-next": ("KernelSU-Next", 0),
    "resukisu": ("ReSukiSU", 700),
}

# `MULTILINE`, because a build log is read whole and every line of it is a candidate: this is also
# what keeps `version fallback:` from being read as a version - it has no colon-and-number after
# the word `version`.
#
# `version code:` is ReSukiSU's spelling (`$(info -- $(REPO_NAME) version code: $(KSU_VERSION))`) and
# `version:` is the other two's, so both are read. `version name:` matches neither, deliberately: it
# carries the tag and the commit, not the number this checks.
REPORTED = re.compile(
    r"-- (?P<project>KernelSU(?:-Next)?|ReSukiSU) version(?: code)?: (?P<version>[0-9]+)[ \t]*\r?$",
    re.MULTILINE,
)
FALLBACK = re.compile(
    r"version fallback: (?P<version>[0-9]+)[ \t]*\r?$"
    r"|KSU_GIT_VERSION not defined"
    # ReSukiSU has no fallback branch at all: without a `.git` its Kbuild stops the build outright,
    # so this is what a log from one says instead of a number.
    r"|You should use \w+ as a git submodule",
    re.MULTILINE,
)


def expected_version(flavour: str, commits: int) -> int:
    """The number a build of [flavour] reports at a commit count of [commits].

    Shared with `check_pair_version.py`, which derives the daemon's number from the same two facts:
    the commit count it reads out of the checkout, and the offset its project adds. A pair agrees by
    construction, so the only way this can be wrong is if the two tools disagreed about the offset.
    """
    _project, offset = FLAVOURS[flavour]
    return VERSION_BASE + offset + commits


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


def self_test() -> int:
    """The flavour table, against the log lines the three projects really print.

    What this protects is the pairing of a number with an offset: ReSukiSU's line read with
    KernelSU's arithmetic is wrong by exactly 700, and nothing in a build log would say so. Each
    flavour's own line is also read with each *other* flavour, where it has to be refused.
    """
    lines = {
        "kernelsu": ("-- KernelSU version: 33294", "KernelSU", 33294),
        "kernelsu-next": ("-- KernelSU-Next version: 33294", "KernelSU-Next", 33294),
        "resukisu": ("-- ReSukiSU version code: 35144", "ReSukiSU", 35144),
    }
    failures = 0
    for flavour, (line, project, version) in lines.items():
        match = REPORTED.search(line)
        if not match:
            print(f"  {flavour}: {line!r} is not read as a version line")
            failures += 1
            continue
        if match.group("project") != project:
            print(f"  {flavour}: read as {match.group('project')}, expected {project}")
            failures += 1
        if int(match.group("version")) != version:
            print(f"  {flavour}: read {match.group('version')}, expected {version}")
            failures += 1

        # The arithmetic, from the count the log's line corresponds to.
        commits = version - expected_version(flavour, 0)
        if expected_version(flavour, commits) != version:
            print(f"  {flavour}: {commits} commits gives {expected_version(flavour, commits)}, not {version}")
            failures += 1

    # A ReSukiSU log line is not a KernelSU one, and the numbers differ by the offset - which is the
    # whole reason the flavour is an input rather than a guess.
    if expected_version("resukisu", 100) - expected_version("kernelsu", 100) != 700:
        print("  resukisu does not add 700 to the count")
        failures += 1

    # The name lines must not be read as numbers: they carry a tag and a commit instead.
    for name_line in ("-- KernelSU version name: 3.3.0", "-- ReSukiSU version name: v4.2.0-rc2"):
        if REPORTED.search(name_line):
            print(f"  {name_line!r} was read as a version")
            failures += 1

    # A log with no number is what a checkout without a `.git` produces, whichever project it is.
    for absent in (
        "-- KernelSU version fallback: 16",
        "-- KSU_GIT_VERSION not defined",
        "-- You should use ReSukiSU as a git submodule instead of copying code directly",
    ):
        if not FALLBACK.search(absent):
            print(f"  {absent!r} is not recognised as a build with no version")
            failures += 1

    print(f"self-test: {len(lines)} flavour(s), {failures} failure(s)")
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--self-test", action="store_true", help="check the flavour table and exit")
    parser.add_argument("--log", help="the build's output, as Kbuild printed it")
    parser.add_argument("--tree", help="the KernelSU checkout the build used")
    parser.add_argument("--receipt", help="write the version this build stamped, for check_pair_version.py")
    parser.add_argument("--ref", help="the tag this build was asked for, recorded in the receipt")
    parser.add_argument(
        "--flavor",
        default="kernelsu",
        choices=sorted(FLAVOURS),
        help="whose arithmetic the number is in, and the project the log has to name",
    )
    arguments = parser.parse_args()

    if arguments.self_test:
        return self_test()
    if not arguments.log or not arguments.tree:
        parser.error("give --log and --tree, or --self-test")

    with open(arguments.log, encoding="utf-8", errors="replace") as handle:
        text = handle.read()

    project, offset = FLAVOURS[arguments.flavor]

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
            f"       version would be {VERSION_BASE + offset + 1} rather than this tag's. Fetch the whole\n"
            "       history for the tag this pair is built from.",
            file=sys.stderr,
        )
        return 1

    expected = expected_version(arguments.flavor, expected_count)
    reads = [(match.group("project"), int(match.group("version"))) for match in REPORTED.finditer(text)]

    if not reads:
        print(
            f"error: the build printed no {project} version line, so it could not be counted.\n"
            f"       The module would report something other than {arguments.flavor}'s own number.",
            file=sys.stderr,
        )
        for line in text.splitlines():
            if FALLBACK.search(line):
                print(f"       {line.strip()[:160]}", file=sys.stderr)
        return 1

    # The name is the one thing the number cannot state: `30000 + count` from the wrong repository is
    # a different KernelSU, and the pair would be built from a tree this flavour does not use.
    named = {name for name, _value in reads}
    if project not in named:
        print(
            f"error: this run is building {arguments.flavor}, whose Kbuild prints\n"
            f"       `-- {project} version...`, but this log names {', '.join(sorted(named))}.\n"
            "       The patch and the repository do not belong to the same flavour.",
            file=sys.stderr,
        )
        return 1

    wrong = sorted({value for _name, value in reads if value != expected})
    if wrong:
        print(
            f"error: the module reports {', '.join(str(value) for value in wrong)}, but this checkout "
            f"is {expected_count} commits\n"
            f"       into its history, which is {expected}. A shallow clone counts 1 and reports "
            f"{VERSION_BASE + offset + 1}, which\n"
            "       looks plausible and is wrong; see the note about --unshallow in "
            "kernel/Kbuild.",
            file=sys.stderr,
        )
        return 1

    offset_text = f" + {offset}" if offset else ""
    print(f"module version {expected} = {VERSION_BASE}{offset_text} + {expected_count} commits at this tag ({project})")

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
