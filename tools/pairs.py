#!/usr/bin/env python3
"""What a KernelSU version bump has to rebuild, derived from what is already published.

A pair is the kernel module plus the `ksud` that embeds it, and the feed entry that serves
them. Rebuilding one for a new KernelSU tag needs four things this repository already knows:
which target it is, which KMI it was built for, which flavour it belongs to, and the kernel
release its module has to claim.

The release is the only input that cannot be invented, because the pair job substitutes it
into the build and then asserts the module's `vermagic` starts with it. This script does not
have to invent it: the pair already published for that target carries it in its own
`vermagic`. Copying that value reproduces exactly the release users are running today, so a
regenerated pair claims what the working one claims, and nothing about the loader's view of
it changes.

Nothing here is maintained by hand. A device port that adds a pair and a feed entry is picked
up by the next run. What cannot be derived is reported rather than guessed at: an entry whose
daemon has no module beside it, and an entry whose module claims a build tree rather than a
device release (the DDK's own `-dirty` release, which the pair job refuses to publish because
substituting the device's release into it is what the build is for).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

# `6.6.98-android15-8-pd6ff1cd-abogkiS938USQSCCZF9-4k SMP preempt ...` -> the release is
# everything before the first space; the rest is the kernel's own configuration trailer.
VERMAGIC = re.compile(rb"vermagic=([^ \x00]+)")

# `<kmi>_kernelsu[-next]-<target>-kdp.ko`, plus the older names that stop at the target.
MODULE = re.compile(
    r"^(?P<kmi>android\d+-\d+\.\d+(?:\.\d+)?)_kernelsu(?P<suffix>-next)?-(?P<target>.+?)(?:-kdp)?\.ko$"
)
# The DDK publishes one image per KMI *family* - `android13-5.15`, `android15-6.6` - and never per
# release, while a module may be named with either. `android13-5.15.189_kernelsu-dm2q-...` is a
# published name, so the release has to come off before the name is used as an image reference: the
# reference is `<family>-<ddk_release>`, and a tag carrying the release cannot exist. The failure
# that causes is a container pull before any step runs, which reads as a missing manifest rather
# than as a tag this repository asked for by mistake.
DDK_KMI = re.compile(r"^(?P<family>android\d+-\d+\.\d+)")
# `ksud[-next]-<target>-kdp`.
DAEMON = re.compile(r"^ksud(?P<suffix>-next)?-(?P<target>.+?)-kdp$")
# The version a payload id carries: `pa3q-S938USQSCCZF9-ksu330`.
VERSIONED_ID = re.compile(r"^(?P<prefix>.+?)-(?P<flavour>ksun?)(?P<version>\d+)$")
# A build tree's own release rather than a device's: `6.6.127-4k-g46a034eca005-dirty`.
BUILD_TREE = re.compile(r"-g[0-9a-f]{7,}(-dirty)?$")
# The version a pair-built daemon carries: `3.4.0 (uapi: 4)`, which is `ksud -V`'s own answer.
DAEMON_VERSION = re.compile(rb"(\d+\.\d+\.\d+) \(uapi: \d+\)")

FLAVOURS = {"": "kernelsu", "-next": "kernelsu-next"}
# `S938USQSCCZF9`: the build id in a payload id or a target id.
BUILD_ID = re.compile(r"^[a-z0-9]+-[A-Z][A-Z0-9]{6,}$")


def version_code(tag: str) -> str | None:
    """`v3.3.0` -> `330`, the form a payload id carries.

    KernelSU's own version code (32601 for 3.3.0) is a build counter the manager prints, not
    something the tag states, so an id uses the tag's own numbers. That is the only shape the
    published ids follow: `ksu330` for v3.3.0.
    """
    parts = re.findall(r"\d+", tag)
    if len(parts) < 2:
        return None
    major, minor, patch = (parts + ["0", "0"])[:3]
    return str(int(major) * 100 + int(minor) * 10 + int(patch))


RELEASE_TEXT = re.compile(r"\b(\d+\.\d+\.\d+-android\d+-[\w.-]+)")


def ddk_family(kmi: str | None) -> str | None:
    """The DDK image a KMI belongs to: the family, without the release a module name may carry.

    Only ever used for the image reference. The name a pair is published under keeps whatever the
    device's modules are already named, because that name is also what the import diff fetches and
    what the feed serves.
    """
    match = DDK_KMI.match(kmi) if kmi else None
    return match.group("family") if match else None


def kmi_for_release(release: str, published: list[str]) -> str | None:
    """The KMI a pair built for this release should be named under, or None when there is no such name.

    A device release reads `5.15.189-android13-8-33413713-abS918BXXSAFZF5`: the version is the device's
    and `android13` is the tree it was built in. Both forms of the name appear in this repository -
    `android13-5.15` for a whole image, `android13-5.15.189` where one image carries more than one
    device release - so the form a sibling is already published under wins, and the image form is the
    fallback. Either resolves to the image the build runs in through `ddk_family`, which is what makes
    a name usable at all: a name that derives no family is one the registry cannot be asked about.

    This is the difference between an entry that has a port document and no module getting a pair of its
    own, and it being reported as if nothing were known about it. The release was never the missing
    part; the name it had to be published under was.
    """
    android = re.search(r"-android(\d+)-", release)
    version = release.split("-")[0]
    if not android or not re.fullmatch(r"\d+\.\d+\.\d+", version):
        return None
    family = f"android{android.group(1)}-{'.'.join(version.split('.')[:2])}"
    if family not in DDK_FAMILIES:
        return None
    exact = f"android{android.group(1)}-{version}"
    for form in (exact, family):
        if any(name.startswith(form + "_") for name in published):
            return form
    return family


def module_release(path: str) -> str | None:
    """The kernel release a built module claims, or None when it carries no vermagic."""
    try:
        with open(path, "rb") as handle:
            match = VERMAGIC.search(handle.read())
    except OSError:
        return None
    return match.group(1).decode("ascii", "replace") if match else None


def daemon_version(path: str) -> str | None:
    """The KernelSU version a daemon was stamped with, or None when it carries none.

    Read from the binary rather than from the entry that serves it, because this is the one place the
    fact is written down for an artifact nobody rebuilt: the feed's `version` field is written by the
    pair job, so an entry the job cannot rebuild has no way to declare one - and a daemon that does not
    say which KernelSU it is cannot be made to, by any amount of editing.

    A hand-built daemon is the case that matters here: the three that serve the shared pairs were built
    before the stamp was part of the build, so nothing in this repository states their version.
    """
    try:
        with open(path, "rb") as handle:
            match = DAEMON_VERSION.search(handle.read())
    except OSError:
        return None
    return match.group(1).decode("ascii", "replace") if match else None


def release_missing_reason(payload_id: str, models: list[str], build: str, stamped: str | None) -> str:
    """Why an entry with no release to copy cannot be rebuilt, naming what would change that.

    Two different situations print the same sentence today, and a maintainer reading it would go
    looking for the wrong thing. One entry naming a single build is a missing document away from
    rebuilding - the release is device truth, so somebody holding the phone has to report it. One entry
    naming a whole series is not: thirty models ship thirty kernels, each pair claims one release, and
    there is no document that could be written to make one entry into one pair.

    The daemon's own stamp is part of the answer either way, because it decides whether a rebuild is
    needed at all: an entry whose daemon says `3.3.0` could declare that today, without being rebuilt.
    """
    if not build:
        lead = (
            f"names {len(models)} model{'s' if len(models) != 1 else ''} whose builds differ, so no "
            "single release can be claimed - a pair per build, not one entry"
        )
    else:
        first = models[0] if models else "<MODEL>"
        lead = f"no release recorded for {build}: add docs/{first}-{build}.md with the device's `uname -r`"
    if stamped:
        return f"{lead} (its daemon says {stamped}, but a shared pair cannot be replaced in place)"
    return f"{lead} (and its daemon carries no version, so a rebuild is the only way it can declare one)"


def documented_release(repo: str, build: str) -> str | None:
    """The release a device port recorded, from `docs/<MODEL>-<BUILD>.md`.

    A port document states what the device reports, which is the one input a pair build cannot
    infer. It is used only where no module is published to copy the value from - that is, where
    the entries share a hand-built pair and a per-target one has to be built instead.
    """
    if not build:
        return None
    suffix = f"-{build}.md"
    for name in sorted(os.listdir(os.path.join(repo, "docs"))):
        if not name.endswith(suffix):
            continue
        with open(os.path.join(repo, "docs", name), encoding="utf-8", errors="replace") as handle:
            found = RELEASE_TEXT.search(handle.read())
        if found:
            return found.group(1)
    return None


def named(pair: dict) -> str:
    """How a pair reads in a report: its name, and the image when the two are not the same."""
    family = pair.get("ddk_kmi")
    if not family or family == pair.get("kmi"):
        return str(pair.get("kmi") or "?")
    return f"{pair['kmi']} (ddk {family})"


def _url(entry: dict, key: str) -> str:
    value = entry.get(key)
    return value.get("url", "") if isinstance(value, dict) else ""


def plan(repo: str, feed: str = "support/targets-v3.json") -> dict:
    """Every pair the feed serves, and what a rebuild of it would use."""
    with open(os.path.join(repo, feed), encoding="utf-8") as handle:
        manifest = json.load(handle)

    artifacts = os.path.join(repo, "kernelsu")
    published: dict[tuple[str, str], dict] = {}
    skipped: list[dict] = []
    migrations: list[dict] = []
    entries: list[dict] = []
    # One 6 MB read per daemon, and several entries share one - which is the whole reason there is a
    # memo here rather than a call at each use.
    stamps: dict[str, str | None] = {}

    def stamp(daemon_name: str) -> str | None:
        if daemon_name not in stamps:
            stamps[daemon_name] = daemon_version(os.path.join(artifacts, daemon_name))
        return stamps[daemon_name]

    for entry in manifest.get("payloads", []):
        payload_id = entry.get("payloadId", "")
        daemon_name = os.path.basename(_url(entry, "kernelsu"))
        daemon = DAEMON.match(daemon_name)
        # An optional group that did not participate is None rather than empty, so the suffix is
        # normalised before it is used as a key.
        suffix = (daemon.group("suffix") or "") if daemon else None
        flavour = FLAVOURS.get(suffix) if suffix is not None else None

        if daemon is None or flavour is None:
            skipped.append({"payloadId": payload_id, "artifact": daemon_name, "reason": "daemon is not named for a target"})
            continue

        target = daemon.group("target")
        entries.append({"payloadId": payload_id, "target": target, "flavor": flavour, "daemon": daemon_name})

        key = (target, flavour)
        if key in published:
            continue

        module_name = None
        kmi = None
        for name in sorted(os.listdir(artifacts)):
            module = MODULE.match(name)
            if not module or module.group("target") != target:
                continue
            if (module.group("suffix") or "") != suffix:
                continue
            kmi, module_name = module.group("kmi"), name
            break

        release = module_release(os.path.join(artifacts, module_name)) if module_name else None
        if release is not None and not BUILD_TREE.search(release):
            # The normal case: the pair already published carries the release to reproduce.
            published[key] = {
                "targetId": target,
                # What the artifact is called, and separately what the build runs in. They differ
                # wherever the published name carries the kernel release.
                "kmi": kmi,
                "ddk_kmi": ddk_family(kmi),
                "release": release,
                "flavor": flavour,
                "module": module_name,
                "daemon": daemon_name,
                # What a rebuild moves away from: the version the daemon serving this pair right now
                # was stamped with, which for a pair that has never been rebuilt is nothing.
                "currentVersion": stamp(daemon_name),
                "payloadIds": [],
            }
            continue

        # Nothing published to copy a release from - either there is no module beside the daemon,
        # or the only one is the build tree's own. Two things can still name the build: the
        # daemon's own target, or the payload id, which for a shared hand-built pair is
        # `<device>-<build>` and is therefore the only place that build is written down.
        # The version a payload id may carry is a suffix, not part of the build it names.
        without_version = re.sub(r"-(ksu|ksun)\d+$", "", payload_id)
        from_id = without_version if BUILD_ID.match(without_version) else ""
        candidate_target = target if "-" in target else from_id
        build = candidate_target.split("-", 1)[1] if "-" in candidate_target else ""
        documented = documented_release(repo, build)
        kmi_from_release = kmi_for_release(documented, os.listdir(artifacts)) if documented else None

        if documented and kmi_from_release:
            own_daemon = f"ksud{'-next' if suffix else ''}-{candidate_target}-kdp"
            migrations.append(
                {
                    "targetId": candidate_target,
                    "kmi": kmi_from_release,
                    "ddk_kmi": ddk_family(kmi_from_release),
                    "release": documented,
                    "flavor": flavour,
                    # The daemon this entry has to move to, which the build produces: a shared pair
                    # cannot be replaced in place, because the artifact is the one four other
                    # entries are still served by.
                    "daemon": daemon_name,
                    "target_daemon": own_daemon,
                    # Which of the two shapes this is. An entry already served by its own daemon is a
                    # pair that was hand-built without a version stamp and with no module beside it, so
                    # a rebuild is a replacement in place; an entry on a shared artifact is a move.
                    "note": "its own daemon already, rebuilt in place"
                    if own_daemon == daemon_name
                    else "a daemon of its own, replacing the shared one",
                    "payloadIds": [payload_id],
                    "module": None,
                    "currentVersion": stamp(daemon_name),
                    "source": "a device port document",
                }
            )
            continue

        if module_name is None:
            skipped.append({"payloadId": payload_id, "artifact": daemon_name, "reason": "no module beside the daemon"})
            continue
        if release is None:
            skipped.append({"payloadId": payload_id, "artifact": module_name, "reason": "module carries no vermagic"})
            continue
        skipped.append(
            {
                "payloadId": payload_id,
                "artifact": module_name,
                "reason": release_missing_reason(
                    payload_id,
                    list(entry.get("models", [])),
                    build,
                    stamp(daemon_name),
                )
                if not documented
                else f"a port document gives {documented}, which no published module builds"
                + (f" (its daemon says {stamp(daemon_name)})" if stamp(daemon_name) else ""),
            }
        )
        continue

    for item in entries:
        pair = published.get((item["target"], item["flavor"]))
        if pair is not None:
            pair["payloadIds"].append(item["payloadId"])

    def version_of(payload_id: str) -> str | None:
        match = VERSIONED_ID.match(payload_id)
        return match.group("version") if match else None

    for group in (published.values(), migrations):
        for pair in group:
            pair["versions"] = sorted({version for version in map(version_of, pair["payloadIds"]) if version})

    key = lambda item: (item["targetId"], item["flavor"])
    return {
        "pairs": sorted(published.values(), key=key),
        "migrations": sorted(migrations, key=key),
        "skipped": skipped,
    }


# Every KMI this repository has ever published a module under, and the image each belongs to.
# The four `5.15` names are the ones that could not be rebuilt: their release was being used as
# the image tag, and there is no `android13-5.15.189-*` for the registry to serve.
DDK_FAMILIES = {
    "android12-5.10": "android12-5.10",
    "android13-5.15": "android13-5.15",
    "android13-5.15.153": "android13-5.15",
    "android13-5.15.189": "android13-5.15",
    "android14-5.15": "android14-5.15",
    "android14-6.1": "android14-6.1",
    "android15-6.6": "android15-6.6",
    "android16-6.12": "android16-6.12",
    "android17-6.18": "android17-6.18",
}


def self_test(repo: str = ".", feed: str = "support/targets-v3.json") -> int:
    """The derivation, checked without needing a registry or a rebuild."""
    failures = 0

    for kmi, expected in DDK_FAMILIES.items():
        actual = ddk_family(kmi)
        if actual != expected:
            print(f"  {kmi}: expected the {expected} image, got {actual}")
            failures += 1

    # A name with no `android<major>-<x>.<y>` prefix is not a KMI, and guessing one from it would
    # send the build to an image chosen by accident.
    for kmi in (None, "", "android13", "13-5.15", "5.15.189", "android-5.15"):
        if ddk_family(kmi) is not None:
            print(f"  {kmi!r}: expected no image, got {ddk_family(kmi)}")
            failures += 1

    # The published shape: a family is two components, and every module name in this repository
    # resolves to one. A name that kept its release is the defect this exists to prevent.
    for name in sorted(_published_module_names()):
        match = MODULE.match(name)
        if not match:
            print(f"  {name}: not a module name this repository publishes")
            failures += 1
            continue
        family = ddk_family(match.group("kmi"))
        if family not in DDK_FAMILIES.values():
            print(f"  {name}: resolves to {family}, which no DDK image is published under")
            failures += 1

    # The name a release publishes under, which is the one thing standing between a documented build
    # and a pair of its own. The two forms are here because both are in the repository, and the choice
    # is made by what a sibling already uses rather than by preference.
    names = sorted(os.listdir(os.path.join(repo, "kernelsu")))
    release_cases = (
        ("5.15.189-android13-8-33413713-abS918BXXSAFZF5", "android13-5.15.189"),
        ("6.6.98-android15-8-g1a2b3c4d5e6f-4k", "android15-6.6"),
        ("5.15.189", None),
        ("6.6.98-notanandroidtree", None),
    )
    for release, expected in release_cases:
        actual = kmi_for_release(release, names)
        if actual != expected:
            print(f"  {release}: expected {expected!r}, got {actual!r}")
            failures += 1

    # Why an entry cannot be rebuilt has two shapes, and the report has to tell them apart: one entry
    # naming a single build is a document away from a pair of its own, and one naming a whole series
    # never will be. The four ids here are the ones the report actually prints them for.
    cases = (
        ("q7q-F966USQU9BZDN", ["SM-F966U", "SM-F966U1"], "F966USQU9BZDN", None, "docs/SM-F966U-F966USQU9BZDN.md"),
        ("dm3q-S918BXXSAFZF5", ["SM-S918B"], "S918BXXSAFZF5", None, "docs/SM-S918B-S918BXXSAFZF5.md"),
        ("galaxy-s25-series-2026-06-07", ["SM-S938B"] * 30, "", None, "a pair per build"),
        ("dm1q-S911U1UES6DYI3", ["SM-S911U1"], "S911U1UES6DYI3", "3.3.0", "its daemon says 3.3.0"),
    )
    for payload_id, models, build, stamped, expected in cases:
        reason = release_missing_reason(payload_id, models, build, stamped)
        if expected not in reason:
            print(f"  {payload_id}: expected {expected!r} in the reason, got {reason!r}")
            failures += 1
    if release_missing_reason("x", ["SM-S938B"], "", None).count("model") != 1:
        print("  one model in a series entry still reads as plural")
        failures += 1

    # The one claim the app acts on: the version the feed declares for an entry has to be the version
    # in the bytes that entry serves. Where both are readable they must agree, because a rebuild writes
    # the field from the tag it was made at and stamps the same tag into the daemon.
    declared = agreed = 0
    path = os.path.join(repo, feed)
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as handle:
            manifest = json.load(handle)
        for entry in manifest.get("payloads", []):
            kernelsu = entry.get("kernelsu")
            version = (kernelsu or {}).get("version") if isinstance(kernelsu, dict) else None
            if not version:
                continue
            declared += 1
            name = os.path.basename(_url(entry, "kernelsu"))
            binary = os.path.join(repo, "kernelsu", name)
            if not os.path.isfile(binary):
                continue
            stamped = daemon_version(binary)
            if stamped is None:
                print(f"  {entry.get('payloadId')}: declares {version}, but its daemon carries no version")
                failures += 1
            elif stamped.lstrip("v") != str(version).lstrip("v"):
                print(f"  {entry.get('payloadId')}: declares {version}, daemon says {stamped}")
                failures += 1
            else:
                agreed += 1

    print(
        f"self-test: {len(DDK_FAMILIES)} KMI name(s), {len(_published_module_names())} module(s), "
        f"{agreed}/{declared} declaration(s) checked against the daemon, {failures} failure(s)"
    )
    return 1 if failures else 0


def _published_module_names() -> list[str]:
    """The module names in this checkout, which is where the names in use are written down."""
    here = os.path.dirname(os.path.abspath(__file__))
    artifacts = os.path.join(os.path.dirname(here), "kernelsu")
    if not os.path.isdir(artifacts):
        return []
    return [name for name in os.listdir(artifacts) if name.endswith(".ko")]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true", help="check the KMI derivation and stop")
    parser.add_argument("--repo", default=".", help="payload repository root")
    parser.add_argument("--feed", default="support/targets-v3.json")
    parser.add_argument("--json", action="store_true", help="print the whole plan as JSON")
    parser.add_argument("--github-matrix", metavar="FLAVOUR", help="print a build matrix for one flavour")
    parser.add_argument("--matrix", action="store_true", help="print the whole build matrix")
    parser.add_argument("--with-migrations", action="store_true", help="include entries ready for a pair of their own")
    parser.add_argument("--write", metavar="PATH", help="write the plan where the workflow can read it")
    arguments = parser.parse_args()

    if arguments.self_test:
        return self_test(arguments.repo, arguments.feed)

    derived = plan(arguments.repo, arguments.feed)

    if arguments.write:
        with open(arguments.write, "w", encoding="utf-8") as handle:
            json.dump(derived, handle, indent=2)
            handle.write("\n")
        print(f"wrote {arguments.write}: {len(derived['pairs'])} pairs, {len(derived['skipped'])} skipped")
        return 0

    if arguments.matrix:
        rows = []
        for pair in derived["pairs"] + (derived["migrations"] if arguments.with_migrations else []):
            rows.append(
                {
                    "targetId": pair["targetId"],
                    # `kmi` names the artifact; `ddk_kmi` is the image the build runs in. A caller
                    # that puts the first where the second belongs asks the registry for a tag that
                    # cannot exist.
                    "kmi": pair["kmi"],
                    "ddk_kmi": pair["ddk_kmi"],
                    "release": pair["release"],
                    "flavor": pair["flavor"],
                    # Empty unless this run is moving the entries onto the new pair, because moving
                    # one changes which artifact a device downloads.
                    "migrate": ",".join(pair["payloadIds"]) if pair in derived["migrations"] else "",
                }
            )
        print(json.dumps(rows, separators=(",", ":")))
        return 0

    if arguments.github_matrix:
        rows = [
            {
                "target_id": pair["targetId"],
                "target_release": pair["release"],
                "kmi": pair["kmi"],
                "ddk_kmi": pair["ddk_kmi"],
                "flavor": pair["flavor"],
                "daemon": pair["daemon"],
                "payload_ids": ",".join(pair["payloadIds"]),
            }
            for pair in derived["pairs"]
            if pair["flavor"] == arguments.github_matrix
        ]
        print(json.dumps(rows, separators=(",", ":")))
        return 0

    if arguments.json:
        print(json.dumps(derived, indent=2))
        return 0

    print(f"pairs the feed serves: {len(derived['pairs'])}")
    for pair in derived["pairs"]:
        served = len(pair["payloadIds"])
        # The version it carries now, which is what a rebuild would move away from - and, for the ones
        # that carry none, the reason their entries cannot declare which KernelSU they stage.
        carries = pair.get("currentVersion") or "no version stamped"
        print(
            f"  {pair['flavor']:14s} {pair['targetId']:24s} {named(pair):22s} "
            f"{pair['release']:56s} serves {served} entr{'y' if served == 1 else 'ies'} ({carries})"
        )
    if derived["migrations"]:
        print(f"ready for a pair of their own, once migrated: {len(derived['migrations'])}")
        for item in derived["migrations"]:
            carries = item.get("currentVersion") or "no version stamped"
            print(
                f"  {', '.join(item['payloadIds']):34s} {item['kmi']:14s} {item['release']} "
                f"-> {item['target_daemon']} ({item['note']}, was {carries})"
            )
    if derived["skipped"]:
        print(f"not rebuildable from here: {len(derived['skipped'])}")
        for item in derived["skipped"]:
            print(f"  {item['payloadId']:34s} {item['reason']} ({item['artifact']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
