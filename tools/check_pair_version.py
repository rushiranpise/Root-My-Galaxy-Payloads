#!/usr/bin/env python3
"""Both halves of a pair have to report the same KernelSU version.

A pair is a module and the daemon that embeds it, and each half derives its own version while it
builds, from the same underlying fact:

| half | where | derivation |
| --- | --- | --- |
| module | `kernel/Kbuild` | `30000 + git rev-list --count HEAD`, printed as `-- KernelSU-Next version:` |
| daemon | `userspace/ksud/build.rs` | the same `30000 + count` as `VERSION_CODE`, and `VERSION_NAME` from `git describe --tags --always` with a leading `v` stripped |

ReSukiSU's two halves do the same thing with one difference, and it is why the flavour is an input:
both add 700 to that count (`30000 + $(KSU_LOCAL_VERSION) + 700`, and
`let version_code = 30000 + 700 + version_code; // For historical reasons`), so a pair built there
reports 30700 + commits. The offset comes from `check_kernel_version.FLAVOURS`, which is also where
the module half gets it, so the two tools cannot disagree about it.

Both read the commit count of the tree they are built in, so on the same commit they agree by
construction - and that is exactly why nothing checked them. The two halves are built in different
jobs, from different clones, and the job that publishes them has neither. Every way a pair ends up
carrying two versions starts there:

- **A moving ref.** `ksu_ref` may be a branch, and the two jobs resolve it minutes apart. The module
  is built at one commit and the daemon stamps another, so the module reports 33294 and the daemon
  33299 — a device reports one number from the kernel and the other from userspace, and the manager
  is compared against the wrong one.
- **A commit past the tag.** `Kbuild` describes its tree with `--abbrev=0`, so it names the nearest
  tag however far back it is; `build.rs` uses the full `git describe`, so the daemon calls itself
  `3.4.0-4-gabc1234`. The feed is then named from the ref, and three names for one build go out.
- **A checkout that cannot count.** Shallow, tagless or not a repository at all: `Kbuild` falls back
  to 1 and `build.rs` to `(0, "0.0.0")`. A daemon reporting `0.0.0` is a version the app reads as
  "no KernelSU", which is a worse failure than either number being wrong.

What this refuses, it refuses before the feed is pointed at anything. It is given the module's
receipt (written by `check_kernel_version.py` beside the module it checked), the daemon's own build
facts, and the daemon binary it produced - so the answer comes from the same git the two builds
read, and is confirmed against the artifact itself rather than assumed.

One thing it deliberately cannot do is read the number back out of the binary. `build.rs` passes
it to rustc rather than writing it anywhere - `defs::VERSION_CODE` is `env!` of a `cargo:rustc-env`
value - and an integer that small has no searchable form in a stripped executable. So the code is
established from the checkout, and the binary only corroborates the name.

At publish time the checkouts are gone, so it runs again on the two receipts. That is a weaker
check by construction - an exact tag cannot be re-established from a receipt - and it says so, which
is why the stronger one is made where the trees still exist.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from check_kernel_version import (  # noqa: E402
    FLAVOURS,
    commits_at,
    exact_tag,
    expected_version,
    head,
    is_shallow,
)

# `v3.4.0-4-g1a879d6a`, which is what `git describe` answers for a commit past the tag.
SUFFIXED = re.compile(r"-\d+-g[0-9a-f]+$")
DOTTED = re.compile(r"\d+\.\d+(?:\.\d+)*")

# The pre-release part of a version name - `4.2.0-rc2`, which is what `git describe` answers for a tag
# that is one. It is part of the version rather than a description of the commit, so it is taken off
# before the dotted test below instead of failing it: refusing it would refuse every pair built by a
# project that ships pre-releases, which is exactly what a project without a released version has.
# `SUFFIXED` is checked first, so a commit past the tag is still told apart from a pre-release tag.
PRERELEASE = re.compile(r"-(?:alpha|beta|rc|pre|preview|milestone)\.?\d*.*$", re.IGNORECASE)


@dataclass(frozen=True)
class Stamp:
    """The version one half would report, and where that answer came from.

    `version` is None when nothing said one, and not 0 - a missing receipt and a daemon that
    stamped build.rs's own fallback are different failures, and 0 is the second of them.
    """

    version: int | None
    name: str
    commit: str
    ref: str
    tag: str | None
    where: str


def read_receipt(path: str) -> dict[str, str]:
    if not os.path.isfile(path):
        # A missing receipt is the shape this takes when a job was skipped or an artifact was not
        # downloaded, so it is named rather than traced.
        raise SystemExit(f"no receipt at {path}")
    fields: dict[str, str] = {}
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            key, separator, value = line.strip().partition("=")
            if separator:
                fields[key.strip()] = value.strip()
    return fields


def module_stamp(path: str) -> Stamp:
    fields = read_receipt(path)
    return Stamp(
        version=int(fields["version"]) if fields.get("version", "").isdigit() else None,
        name="",
        commit=fields.get("commit", ""),
        ref=fields.get("ref", ""),
        tag=fields.get("tag") or None,
        where=path,
    )


def check_binary(name: str, binary: str) -> None:
    """The one thing about the binary that can be read where it cannot be run.

    `defs::VERSION_NAME` is `env!` of a `cargo:rustc-env` value, so the name is a compiled-in string
    and its presence is checkable by looking. Nothing here proves the binary is *this* build of the
    crate - a dependency's version string could satisfy it - which is why it accompanies the receipt
    rather than replacing it. The code is an integer immediate and is not searched for: a byte
    pattern that short would match anything.
    """
    if not os.path.isfile(binary):
        raise SystemExit(f"no daemon binary at {binary}")
    with open(binary, "rb") as handle:
        blob = handle.read()
    if name.encode() not in blob:
        raise SystemExit(f"{binary} does not contain {name!r}, the version this check just derived")
    print(f"  {os.path.basename(binary)} carries {name}")


def daemon_stamp(receipt: str | None, tree: str | None, binary: str | None, flavour: str = "kernelsu") -> Stamp:
    """The daemon's version, from the receipt when publishing and from the tree when building.

    The tree is preferred because it is where the number actually comes from: `build.rs` reads
    `git rev-list --count HEAD` and `git describe --tags --always` of the checkout it compiles in,
    and this reads the same two things rather than a transcription of them. Which project's
    arithmetic that count is in is [flavour]'s business - see [expected_version].
    """
    if tree is None:
        if receipt is None:
            raise SystemExit("give --daemon-tree (building) or --daemon-receipt (publishing)")
        fields = read_receipt(receipt)
        stamp = Stamp(
            version=int(fields["version"]) if fields.get("version", "").isdigit() else None,
            name=fields.get("name", ""),
            commit=fields.get("commit", ""),
            ref=fields.get("ref", ""),
            tag=fields.get("tag") or None,
            where=receipt,
        )
        if binary is not None and stamp.name:
            check_binary(stamp.name, binary)
        return stamp

    if is_shallow(tree):
        raise SystemExit(
            f"{tree} is a shallow clone, so it counts 1 commit and would stamp "
            f"{expected_version(flavour, 1)}.\n"
            "Fetch the whole history for the tag this pair is built from."
        )
    count = commits_at(tree)
    commit = head(tree)
    if count is None or commit is None:
        raise SystemExit(f"{tree} is not a git checkout to count commits in")

    described = subprocess.run(
        ["git", "-C", tree, "describe", "--tags", "--always"], capture_output=True, text=True, check=False
    ).stdout.strip()
    if not described:
        raise SystemExit(f"{tree} has nothing for `git describe` to answer with")
    # `trim_start_matches('v')` in build.rs strips every leading `v`, so this does too.
    name = described.lstrip("v")

    version = expected_version(flavour, count)
    tag = exact_tag(tree)

    if binary is not None:
        check_binary(name, binary)

    return Stamp(version=version, name=name, commit=commit, ref="", tag=tag, where=tree)


def verdict(module: Stamp, daemon: Stamp, ref: str | None) -> str | None:
    """`None` when the two halves agree, otherwise the name of the first disagreement.

    Order matters only for which message a pair gets when it is wrong in more than one way: the
    fallbacks come first because a `0.0.0` daemon fails every comparison below it, and saying
    "the checkout could not be counted" is more use than "the numbers differ".
    """
    if module.version is None or daemon.version is None:
        return "incomplete"
    if not module.commit or not daemon.commit or not daemon.name:
        return "incomplete"
    if module.version == 1:
        return "module-fallback"
    if daemon.version == 0 or daemon.name == "0.0.0":
        return "daemon-fallback"
    if module.version != daemon.version:
        return "version"
    if module.commit != daemon.commit:
        return "commit"
    # Suffix first: `3.4.0-4-g1a879d6a` also fails the dotted test, and "a commit past the tag" is a
    # more useful thing to be told than "that is not a version".
    if SUFFIXED.search(daemon.name):
        return "suffix"
    if not DOTTED.fullmatch(PRERELEASE.sub("", daemon.name)):
        return "name"
    if ref:
        expected = ref.lstrip("v")
        if daemon.name != expected:
            return "ref"
        if module.tag and module.tag != ref:
            return "module-tag"
        if module.ref and module.ref != ref:
            return "module-ref"
    return None


def explain(reason: str, module: Stamp, daemon: Stamp, ref: str | None) -> str:
    if reason == "incomplete":
        return (
            "one of the two halves did not say what it stamped:\n"
            f"  module {module.where}: version {module.version if module.version is not None else '(none)'}, "
            f"commit {module.commit or '(none)'}\n"
            f"  daemon {daemon.where}: version {daemon.version if daemon.version is not None else '(none)'}, "
            f"name {daemon.name or '(none)'}, "
            f"commit {daemon.commit or '(none)'}"
        )
    if reason == "module-fallback":
        return (
            "the module was built with Kbuild's fallback number, 1, which means its checkout could not\n"
            "be counted at all."
        )
    if reason == "daemon-fallback":
        return (
            "the daemon was stamped with build.rs's fallback (0 / 0.0.0), which means its checkout had\n"
            "no history to count. The app reads 0.0.0 as no KernelSU at all."
        )
    if reason == "version":
        return (
            f"the module reports {module.version} and the daemon would report {daemon.version}.\n"
            "  Both are this project's base plus the commit count of the tree they were built in, so the\n"
            "  two jobs built different commits — the usual cause is a branch given as the ref, resolved\n"
            "  twice — or the two halves were built from different projects' arithmetic."
        )
    if reason == "commit":
        return (
            f"the two halves were built at different commits:\n"
            f"  module {module.commit}\n"
            f"  daemon {daemon.commit}"
        )
    if reason == "name":
        return (
            f"the daemon would call itself {daemon.name!r}, which names no version.{' A tagless checkout makes `git describe --always` answer with a bare commit hash.' if DOTTED.search(daemon.name) is None else ''}"
        )
    if reason == "suffix":
        return (
            f"the daemon would call itself {daemon.name!r} — a commit past the tag, not the tag.\n"
            "  `Kbuild` names the nearest tag however far back it is, so the module would carry the\n"
            "  tag while the daemon carries the tag plus the commits since it, and the feed is named\n"
            "  from the ref. Build the pair at the tag."
        )
    if reason == "ref":
        return (
            f"this run is building {ref}, but the daemon would call itself {daemon.name!r}"
            f" (expected {(ref or '').lstrip('v')!r})."
        )
    if reason == "module-tag":
        return (
            f"the module was built at {module.tag}, not at the {ref} this run asked for."
        )
    if reason == "module-ref":
        return f"the module's receipt was written for {module.ref}, not for the {ref} this run asked for."
    return reason


def self_test() -> int:
    """The decision table, on stamps that need no repository to build.

    Every failure this tool exists for is a disagreement between two small records, so each of them
    can be stated here without a checkout, a compiler, or a tag. The cases are the three shapes in
    the module docstring, one per line, and the test fails if any of them stops being refused.
    """
    good = dict(version=33294, commit="1a879d6a", ref="v3.4.0", tag="v3.4.0")
    daemon_good = Stamp(version=33294, name="3.4.0", commit="1a879d6a", ref="", tag="v3.4.0", where="tree")

    def module(**overrides) -> Stamp:
        fields = {**good, **overrides}
        return Stamp(name="", where="receipt", **fields)

    cases: list[tuple[str, Stamp, Stamp, str | None, str | None]] = [
        ("a pair that agrees", module(), daemon_good, "v3.4.0", None),
        (
            "a shallow daemon checkout (30001)",
            module(),
            Stamp(version=30001, name="3.4.0", commit="1a879d6a", ref="", tag=None, where="tree"),
            "v3.4.0",
            "version",
        ),
        (
            "a branch resolved twice (different counts)",
            module(version=33294, commit="1a879d6a"),
            Stamp(version=33299, name="3.4.0", commit="abcdef12", ref="", tag=None, where="tree"),
            "v3.4.0",
            "version",
        ),
        (
            "same count, different commits",
            module(version=33294, commit="1a879d6a"),
            Stamp(version=33294, name="3.4.0", commit="abcdef12", ref="", tag=None, where="tree"),
            "v3.4.0",
            "commit",
        ),
        (
            # Same commit, so the same count on both sides - what differs is the name, because
            # `Kbuild` names the nearest tag and `build.rs` names the tag plus the commits since it.
            "a commit past the tag",
            module(version=33298, tag="v3.4.0"),
            Stamp(version=33298, name="3.4.0-4-g1a879d6a", commit="1a879d6a", ref="", tag=None, where="tree"),
            "v3.4.0",
            "suffix",
        ),
        (
            "a tagless daemon checkout",
            module(),
            Stamp(version=33294, name="1a879d6a", commit="1a879d6a", ref="", tag=None, where="tree"),
            "v3.4.0",
            "name",
        ),
        (
            # A project that ships pre-releases has no other kind of tag, so its version name is one
            # and the pair is right: `4.2.0-rc2` is a version, `1a879d6a` is not.
            "a pre-release tag",
            module(version=30700 + 3514, ref="v4.2.0-rc2", tag="v4.2.0-rc2"),
            Stamp(version=30700 + 3514, name="4.2.0-rc2", commit="1a879d6a", ref="", tag="v4.2.0-rc2", where="tree"),
            "v4.2.0-rc2",
            None,
        ),
        (
            "a pre-release name this run did not ask for",
            module(),
            Stamp(version=33294, name="4.2.0-rc2", commit="1a879d6a", ref="", tag=None, where="tree"),
            "v3.4.0",
            "ref",
        ),
        (
            "the daemon's own fallback",
            module(),
            Stamp(version=0, name="0.0.0", commit="1a879d6a", ref="", tag=None, where="tree"),
            "v3.4.0",
            "daemon-fallback",
        ),
        (
            "the module's own fallback",
            module(version=1),
            daemon_good,
            "v3.4.0",
            "module-fallback",
        ),
        (
            "the daemon is a different release",
            module(),
            Stamp(version=33214, name="3.3.0", commit="1a879d6a", ref="", tag=None, where="tree"),
            "v3.4.0",
            "version",
        ),
        (
            "the module's receipt is for another tag",
            module(tag="v3.3.0"),
            daemon_good,
            "v3.4.0",
            "module-tag",
        ),
        (
            "an empty receipt",
            module(version=None, tag=None, commit=""),
            daemon_good,
            "v3.4.0",
            "incomplete",
        ),
        (
            "no receipt at all on the daemon side either",
            module(),
            Stamp(version=None, name="", commit="", ref="", tag=None, where="receipt"),
            "v3.4.0",
            "incomplete",
        ),
    ]

    failures = 0
    for label, mod, dae, ref, expected in cases:
        found = verdict(mod, dae, ref)
        if found != expected:
            print(f"FAILED: {label}: expected {expected!r}, got {found!r}")
            failures += 1
        else:
            print(f"  {'refused' if expected else 'accepted'}: {label}" + (f" ({found})" if found else ""))

    print(f"{len(cases) - failures}/{len(cases)} cases")

    # The offset, held against the module half's own table rather than against a second copy of the
    # same constant: a pair built by ReSukiSU is exactly 700 above a KernelSU pair at one commit, and
    # a check read with the wrong arithmetic is off by that much on every build.
    for commits in (0, 1, 3514):
        gap = expected_version("resukisu", commits) - expected_version("kernelsu", commits)
        if gap != FLAVOURS["resukisu"][1]:
            print(f"FAILED: resukisu at {commits} commits is {gap} above kernelsu, not 700")
            failures += 1
    if FLAVOURS["kernelsu-next"][1] != 0:
        print("FAILED: kernelsu-next is not on the shared base")
        failures += 1
    print(f"offsets: {', '.join(f'{name}+{offset}' for name, (_project, offset) in sorted(FLAVOURS.items()))}")

    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--self-test", action="store_true", help="run the decision table and exit")
    parser.add_argument("--module-receipt", help="written by check_kernel_version.py beside the module")
    parser.add_argument("--daemon-tree", help="the checkout the daemon was built in")
    parser.add_argument("--daemon-receipt", help="the daemon's own receipt, when its tree is gone")
    parser.add_argument("--daemon-binary", help="the built daemon, to check the name inside it")
    parser.add_argument("--ref", help="the tag this run is building, e.g. v3.4.0")
    parser.add_argument(
        "--flavor",
        default="kernelsu",
        choices=sorted(FLAVOURS),
        help="whose arithmetic the daemon's number is in",
    )
    parser.add_argument("--receipt", help="write the daemon's receipt where publish can read it")
    arguments = parser.parse_args()

    if arguments.self_test:
        return self_test()
    if not arguments.module_receipt:
        parser.error("give --module-receipt, or --self-test")

    module = module_stamp(arguments.module_receipt)
    daemon = daemon_stamp(
        arguments.daemon_receipt, arguments.daemon_tree, arguments.daemon_binary, arguments.flavor
    )
    ref = arguments.ref or module.ref or None

    if arguments.receipt:
        # Written before the verdict, deliberately: a receipt that only exists when the check passes
        # is a receipt the publish job cannot use to explain what it received.
        with open(arguments.receipt, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(f"version={daemon.version}\n")
            handle.write(f"name={daemon.name}\n")
            handle.write(f"commit={daemon.commit}\n")
            handle.write(f"ref={ref or ''}\n")
            handle.write(f"tag={daemon.tag or ''}\n")
        print(f"wrote {arguments.receipt}")

    reason = verdict(module, daemon, ref)
    if reason is not None:
        print(f"error: {explain(reason, module, daemon, ref)}", file=sys.stderr)
        return 1

    print(f"pair version {module.version} ({daemon.name}) at {module.commit[:12]} - module and daemon agree")
    if arguments.daemon_tree is None:
        print("  (receipts only: the exact-tag assertion was made where the checkout still existed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
