#!/usr/bin/env python3
"""Carry a flavour's Samsung patch onto a new upstream tag, by merge rather than by line numbers.

A patch keyed to one tag stops applying as soon as upstream moves the lines around its anchors, and
until now that was the end of the run: the watcher tried `git apply`, failed, and filed an issue -
even when what changed was that upstream inserted a block *beside* ours. A three-way merge resolves
that by content rather than by line number, so a release that only rearranged its neighbourhood
lands here without a person.

What a merge cannot do is decide. Where both sides change the same lines, `git apply -3` writes
conflict markers, and this tool will not guess an answer to that: a wrong resolution is a kernel
module that compiles wrong or not at all, and the version it replaces is one a device has already
run. It resolves one shape and only one - both sides *adding* to the ancestor, which is upstream's
`ifeq` block beside ours, or two `config` entries in one list - and escalates the rest with the
hunks, leaving the merged tree in place so the decision is read rather than reconstructed.

Modes, in the order they are tried:

    strict      the patch applies to the tag as it stands
    merged      it does not, and a three-way merge settles it with no conflict
    resolved    the merge conflicted only where both sides add to the ancestor
    conflict    a conflict needs a person's decision: exit 2, with the tree left as merged

What comes out is regenerated from the tag, not from the previous patch file, and it is checked by
applying it strictly to a fresh checkout before it is handed back. That check is not ceremony: the
old path regenerated the patch with `git diff`, which does not see files the patch *adds*, so a
rebase of a patch that adds files would have quietly dropped every one of them.
"""

from __future__ import annotations

import argparse
import dataclasses
import difflib
import os
import re
import subprocess
import sys

# `git apply -3` labels its sides on the marker lines. Ours is the tree as the new tag has it; theirs
# is the result the patch asks for. Either label text is accepted, including none.
MARKER_OURS = re.compile(r"^<{7}(?: |$)")
MARKER_BASE = re.compile(r"^\|{7}(?: |$)")
MARKER_SEPARATOR = re.compile(r"^={7}$")
MARKER_THEIRS = re.compile(r"^>{7}(?: |$)")

MISSING_BLOB = "lacks the necessary blob"

EXIT_OK = 0
EXIT_CONFLICT = 2
EXIT_BAD_PATCH = 3
EXIT_CANNOT_MERGE = 4


@dataclasses.dataclass
class Conflict:
    """One conflicted region, as the markers in a file describe it."""

    start: int
    """Index of the `<<<<<<<` line."""

    end: int
    """Index one past the `>>>>>>>` line."""

    ours: list[str]
    theirs: list[str]
    base: list[str] | None

    def resolvable(self) -> bool:
        """Whether both sides only *add* lines to the ancestor, or touch what it had.

        That is the whole test, and the ancestor is what makes it decidable. Two sides that only add
        are two additions to one list - upstream's `ifeq` block and ours, two `config` entries - and
        the answer is both, in order. A side that rewrites or drops an ancestor line is saying
        something about that line, and where the other side also has something to say about it, the
        two answers are alternatives rather than additions.

        `kernel/core/init.c` is the worked example: the ancestor carries `#if defined(__x86_64__)`,
        upstream narrows it to exclude the x86 dispatcher patch, and the Samsung patch inserts
        `int ret;` at that same anchor. Only the ancestor tells you that the patch *kept* that line
        as context while upstream rewrote it - without it, the two look like independent additions,
        and keeping both emits two `#if`s and an `int ret;` inside the first one. That is the failure
        a content-based test produced here, and a boundary-based one accepted a one-line rewrite
        (the v3.3.0 `utils::install(None)` -> `finish_install(None)` signature conflict) as if it
        were two additions, which is the same bug in a worse place.
        """
        if self.base is None:
            return False
        if not self.ours or not self.theirs:
            return False
        return pure_insertion(self.base, self.ours) and pure_insertion(self.base, self.theirs)


def pure_insertion(base: list[str], side: list[str]) -> bool:
    """Whether `side` is `base` with lines added, and nothing else done to it.

    A side identical to the ancestor counts as no change, not as an addition: keeping both sides of
    such a region would repeat the ancestor's own lines, so it is escalated instead.
    """
    matcher = difflib.SequenceMatcher(a=base, b=side, autojunk=False)
    operations = [opcode for opcode, *_ in matcher.get_opcodes()]
    return any(opcode == "insert" for opcode in operations) and all(
        opcode in ("equal", "insert") for opcode in operations
    )


def conflicts_in(text: str) -> list[Conflict]:
    """Every conflict the markers describe, in order.

    A file whose markers do not parse - an incomplete region, which is what a file somebody saved
    half-edited looks like - comes back as one conflict covering the rest of the file, so it is
    escalated rather than resolved.
    """
    lines = text.splitlines(keepends=True)
    found: list[Conflict] = []
    index = 0
    while index < len(lines):
        if not MARKER_OURS.match(lines[index]):
            index += 1
            continue
        start = index
        index += 1
        ours: list[str] = []
        while index < len(lines) and not MARKER_SEPARATOR.match(lines[index]) and not MARKER_BASE.match(lines[index]):
            ours.append(lines[index])
            index += 1
        base: list[str] | None = None
        if index < len(lines) and MARKER_BASE.match(lines[index]):
            index += 1
            base = []
            while index < len(lines) and not MARKER_SEPARATOR.match(lines[index]):
                base.append(lines[index])
                index += 1
        if index >= len(lines) or not MARKER_SEPARATOR.match(lines[index]):
            return [Conflict(start, len(lines), ours, [], None)]
        index += 1
        theirs: list[str] = []
        while index < len(lines) and not MARKER_THEIRS.match(lines[index]):
            theirs.append(lines[index])
            index += 1
        if index >= len(lines):
            return [Conflict(start, len(lines), ours, [], None)]
        found.append(Conflict(start, index + 1, ours, theirs, base))
        index += 1
    return found


def analyzed(text: str) -> tuple[str | None, int, list[Conflict]]:
    """A conflicted file resolved, or None with the regions that need a decision.

    All or nothing per file: the whole file is left to a person as soon as one of its regions does,
    because a file resolved in part is a file nobody can review - the guessed part is no longer
    distinguishable from the merged part.

    Ours goes first. For the shape this accepts the order is a formatting choice rather than a
    semantic one - two additions to a list - and keeping upstream's lines where upstream put them is
    what makes the result read like the file it came from.
    """
    found = conflicts_in(text)
    independent = [conflict for conflict in found if conflict.resolvable()]
    unresolved = [conflict for conflict in found if not conflict.resolvable()]
    if unresolved:
        return None, len(independent), unresolved
    lines = text.splitlines(keepends=True)
    # Replaced from the end, so the earlier indices stay valid as the lines are swapped out.
    for conflict in sorted(independent, key=lambda item: item.start, reverse=True):
        lines[conflict.start : conflict.end] = conflict.ours + conflict.theirs
    return "".join(lines), len(independent), []


def git(tree: str, *arguments: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", tree, *arguments], capture_output=True, text=True, check=False)


def untouched(tree: str) -> None:
    """A tag's own tree, whatever a previous attempt left in the clone."""
    git(tree, "reset", "--hard", "--quiet", "HEAD")
    git(tree, "clean", "--quiet", "-fd")


def conflicted_files(tree: str) -> list[str]:
    result = git(tree, "diff", "--name-only", "--diff-filter=U")
    return [line for line in result.stdout.splitlines() if line.strip()]


def report_of(
    mode: str,
    outcomes: list[tuple[str, str]],
    conflicts: list[tuple[str, Conflict, str]],
    note: str = "",
) -> str:
    """What happened, in the form an issue body can carry as it stands."""
    lines = [f"**Rebase mode: `{mode}`**", ""]
    if note:
        lines += [note, ""]
    for path, outcome in outcomes:
        lines.append(f"- `{path}` — {outcome}")
    lines.append("")
    for path, conflict, text in conflicts:
        lines += [
            f"### `{path}` needs a decision",
            "",
            "`<<<<<<< ours` is the new tag, `||||||| base` is the ancestor they both edited, and",
            "`>>>>>>> theirs` is what the patch adds. At least one side changes a line the ancestor",
            "had, so the two are alternatives rather than two additions - and which one is wanted is",
            "a decision, not a merge.",
            "",
            "```diff",
            text.rstrip("\n")[:6000],
            "```",
            "",
        ]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tree", required=True, help="a checkout of the new tag, at that tag")
    parser.add_argument("--patch", required=True, help="the patch to carry forward")
    parser.add_argument("--out", required=True, help="where to write the patch for the new tag")
    parser.add_argument("--report", help="where to write a markdown account of what happened")
    parser.add_argument("--dry-run", action="store_true", help="do not write the patch")
    arguments = parser.parse_args()

    # Made absolute here rather than left to the caller: `git -C` changes the directory git resolves
    # a patch path against, so a path that is right from the caller's side is not the one git opens.
    tree, patch, out = (os.path.abspath(item) for item in (arguments.tree, arguments.patch, arguments.out))
    if not os.path.isfile(patch):
        print(f"error: no patch at {patch}", file=sys.stderr)
        return EXIT_BAD_PATCH
    # The tree is reset before anything is merged, so a patch kept inside it would be a file the
    # tool deletes and then asks for. And a patch left in the tree would be swept into the
    # regenerated diff as a file the patch adds, which is a patch that carries itself.
    for label, path in (("patch", patch), ("output", out)):
        if path.startswith(tree + os.sep):
            print(
                f"error: the {label} is inside the tree being merged ({path}).\n"
                "       Put it outside: the tree is reset, and anything untracked left in it ends up\n"
                "       in the regenerated patch.",
                file=sys.stderr,
            )
            return EXIT_BAD_PATCH
    untouched(tree)

    def finish(mode: str, outcomes, conflicts, note="", detail="") -> int:
        if arguments.report:
            with open(arguments.report, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(report_of(mode, outcomes, conflicts, "\n\n".join(part for part in (note, detail) if part)))
        print(f"rebase: mode={mode}")
        for path, outcome in outcomes:
            print(f"  {path}: {outcome}")
        return {"strict": EXIT_OK, "merged": EXIT_OK, "resolved": EXIT_OK, "conflict": EXIT_CONFLICT}[mode]

    strict = git(tree, "apply", "--check", patch)
    if strict.returncode == 0:
        git(tree, "apply", patch)
        mode, outcomes = "strict", [("(the patch)", "applies to this tag unchanged")]
    else:
        # `diff3` so every conflict carries the ancestor as well as the two sides: the decision this
        # tool makes is about what each side did to that text, and git already knows it.
        merged = git(tree, "-c", "merge.conflictStyle=diff3", "apply", "-3", patch)
        if MISSING_BLOB in merged.stderr:
            print(
                "error: the clone cannot serve a 3-way merge - it was made with a blob filter.\n"
                "       Clone the tag without --filter so the patch's own ancestor is in the object\n"
                "       database; it is 26 MB and a few seconds.",
                file=sys.stderr,
            )
            return EXIT_CANNOT_MERGE

        # A merge can also fail without conflicting: when the ancestry is too far apart to line a
        # hunk up at all, git skips that file and says so. Those hunks are not in the tree, and a
        # patch regenerated from it would be the delta with them missing - so the run stops here
        # rather than publishing a patch that quietly lost the Samsung delta from one file.
        failures = [
            line
            for line in f"{merged.stdout}{merged.stderr}".splitlines()
            if line.startswith("error:")
        ]
        if failures:
            return finish(
                "conflict",
                [("(the patch)", f"{len(failures)} hunk(s) did not land")],
                [],
                "The merge could not place part of the patch. git's own output:",
                "```\n" + "\n".join(failures[:60]) + "\n```",
            )

        marker_files = conflicted_files(tree)
        if not marker_files:
            mode, outcomes = "merged", [("(the patch)", "merged three ways with no conflict")]
        else:
            entries: list[tuple[str, str]] = []
            unresolved: list[tuple[str, Conflict, str]] = []
            rewritten_files: list[tuple[str, str]] = []
            for path in marker_files:
                full = os.path.join(tree, path)
                with open(full, encoding="utf-8", errors="surrogateescape", newline="") as handle:
                    text = handle.read()
                rewritten, settled, unresolved_here = analyzed(text)
                if rewritten is None:
                    split = text.splitlines(keepends=True)
                    for conflict in unresolved_here:
                        unresolved.append((path, conflict, "".join(split[conflict.start : conflict.end])))
                    touched = f"{len(unresolved_here)} touching an ancestor line"
                    entries.append((path, f"{settled} additive, {touched}" if settled else touched))
                    continue
                rewritten_files.append((full, rewritten))
                entries.append((path, f"{settled} conflict(s), each side only adds lines"))
            if unresolved:
                # Left exactly as the merge made it, markers and all: whoever picks this up should
                # start from a merge rather than from a rejected log.
                return finish(
                    "conflict",
                    entries,
                    unresolved,
                    "Nothing was written: the clone holds the three-way merge as it stands, with the "
                    "conflicts above marked in it.",
                )
            for full, text in rewritten_files:
                with open(full, "w", encoding="utf-8", errors="surrogateescape", newline="") as handle:
                    handle.write(text)
            mode, outcomes = "resolved", entries

    # `-N` so that files the patch adds are part of the diff: plain `git diff` omits untracked
    # files, which is how a rebase of this patch would have lost the four Samsung sources.
    git(tree, "add", "-A", "-N", ".")
    regenerated = git(tree, "diff", "HEAD")
    if regenerated.returncode != 0 or not regenerated.stdout.strip():
        print(f"error: nothing to write - the merge produced no changes{regenerated.stderr}", file=sys.stderr)
        return EXIT_BAD_PATCH
    if not arguments.dry_run:
        with open(out, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(regenerated.stdout)

    # The build jobs apply the patch strictly, with no merge behind them, so it only counts as
    # finished if it lands that way on a checkout nobody has touched. This is also what proves the
    # regeneration kept everything the merge produced.
    untouched(tree)
    verified = git(tree, "apply", "--check", out)
    if verified.returncode != 0:
        print(
            "error: the regenerated patch does not apply to the tag it was made from, so it is not\n"
            f"       the delta it claims to be:\n{verified.stderr}",
            file=sys.stderr,
        )
        return EXIT_BAD_PATCH

    note = f"Verified: `{os.path.basename(out)}` applies to this tag with no merge behind it."
    return finish(mode, outcomes, [], note)


if __name__ == "__main__":
    sys.exit(main())
