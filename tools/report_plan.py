#!/usr/bin/env python3
"""What a rebuild did not cover, as a run summary.

A run that silently rebuilds thirteen of twenty-two feed entries is worse than one that says
which nine it left, because the nine are the ones somebody has to do something about. Two kinds
are reported, and they need different things:

- **Skipped**: no pair can be derived. Either nothing is published to copy a release from, or a
  device port document has never named the build, or the entry is served by a hand-built pair
  the pair job refuses to substitute into.
- **Ready for a pair of their own**: a port document names the build, so the pair can be built -
  but the entry still has to be moved onto it, which changes the artifact a device downloads and
  therefore only happens when a run asks for it.
"""

from __future__ import annotations

import argparse
import json
import os


def named(pair: dict) -> str:
    """A pair's KMI, with the DDK image beside it when the name and the image are not the same.

    They differ wherever the published artifact carries the kernel release (`android13-5.15.189`)
    while the image is per family (`android13-5.15`). Both are worth seeing here: the name is what
    a device downloads, and the image is the half that cannot be chosen wrongly.
    """
    family = pair.get("ddk_kmi")
    if not family or family == pair.get("kmi"):
        return str(pair.get("kmi") or "?")
    return f"{pair['kmi']} (ddk {family})"


def markdown(plan: dict) -> str:
    lines: list[str] = []
    pairs = plan.get("pairs", [])
    lines.append(f"### Rebuilt by this run\n\n{len(pairs)} pair(s) the feed serves.")

    served = [pair for pair in pairs if pair.get("ddk_kmi") != pair.get("kmi")]
    if served:
        lines.append("\nThe image a pair builds in is the KMI family, which is not always the name it is")
        lines.append("published under:\n")
        lines.append("| pair | artifact KMI | build image |")
        lines.append("| --- | --- | --- |")
        for pair in served:
            lines.append(
                f"| `{pair['targetId']}` | `{pair['kmi']}` | `{pair['ddk_kmi']}` |"
            )

    if plan.get("migrations"):
        lines.append("\n### Ready for a pair of their own\n")
        lines.append("A device port document names these builds, so they can be built - but each entry")
        lines.append("still points at a shared hand-built pair until a run is asked to move it.\n")
        lines.append("| entry | kmi | release | would move to |")
        lines.append("| --- | --- | --- | --- |")
        for item in plan["migrations"]:
            lines.append(
                f"| `{', '.join(item['payloadIds'])}` | `{named(item)}` "
                f"| `{item['release']}` | `{item['target_daemon']}` |"
            )

    if plan.get("skipped"):
        lines.append("\n### Not rebuilt\n")
        lines.append("| entry | why | artifact |")
        lines.append("| --- | --- | --- |")
        for item in plan["skipped"]:
            lines.append(f"| `{item['payloadId']}` | {item['reason']} | `{item['artifact']}` |")

    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", help="the plan file written by pairs.py --write")
    parser.add_argument("--out", help="write here instead of stdout")
    arguments = parser.parse_args()

    with open(arguments.plan, encoding="utf-8") as handle:
        plan = json.load(handle)
    text = markdown(plan)
    if arguments.out:
        with open(arguments.out, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
