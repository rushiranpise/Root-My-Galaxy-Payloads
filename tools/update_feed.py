#!/usr/bin/env python3
"""Point the feed at a pair that has just been built.

The last manual step of a KernelSU bump is editing `support/targets-v3.json`: the new daemon's
size and digest, and the version its payload id and display name carry. That is mechanical, so
this does it, and refuses rather than guesses when the entry it would edit is ambiguous.

Two shapes of change, and the difference matters:

- **Republish**: the entry already names the daemon that was rebuilt (`ksud-<target>-kdp`), so
  its URL keeps its directory and only the file changes - plus the size and digest.
- **Migrate**: several entries can be served by one hand-built pair (`ksud-s25u-kdp`). Those
  cannot be republished in place, because that artifact is what the other entries are still
  served by; each gets a pair of its own instead, and its URL moves with it. This only happens
  when the target and its release were derived from a device port document, and the entry is
  named for exactly one build.
- **Create**: the target has no entry for this flavour yet, so one is added. That is how a second
  flavour reaches a device that has only ever been served the first: the entry it already has is
  the device row, and the new one is that row pointed at the new pair. Creation is part of the
  publish run rather than a step before it, deliberately - an entry whose daemon does not exist yet
  is a payload the app will offer and fail to download, so the entry and the artifact it names have
  to arrive in the same commit. Only reachable with `--create`, and only from an entry already
  serving `ksud-<target>-kdp`, so a daemon name that is merely wrong is still refused.

**Against a flavour's own daemon**, only the id and the display name are invented where an entry is
created: the payload id gains the flavour's prefix and the release's number (`dm3q-S918BXXSAFZF5` ->
`dm3q-S918BXXSAFZF5-ksun340`, matching `pa3q-S938USQSCCZF9-ksun340`), and the label gains the
flavour and the release it was built from. Everything else - the models, the kernel versions, the
exploit, and any device field this tool has never heard of - is the sibling's own text, rewritten
rather than reconstructed.

A URL is never rewritten from scratch: the existing one supplies its own prefix, which is how
the app's allowed-repository rule is satisfied, and the file name is the only part replaced.

It also writes `kernelsu.version`: the release the daemon beside it was built from. The payload id
spells that version into a name (`ksun340`), but a name is not a fact the app can compare against a
manager's own version, and the app needs it to offer the manager that belongs to the KernelSU a run
will actually stage. `--backfill` writes that field for entries whose id already carries a version
and which are not being rebuilt.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pairs import DAEMON, FLAVOURS, ID_PREFIXES, VERSIONED_ID, version_code  # noqa: E402

# What a flavour is called where a person reads it. Only used to label an entry that is being
# created - a republished one is retitled, never renamed.
FLAVOUR_LABELS = {"kernelsu": "KernelSU", "kernelsu-next": "KernelSU-Next", "resukisu": "ReSukiSU"}


def _suffix_of(payload_id: str) -> tuple[str, str] | None:
    """`pa3q-S938USQSCCZF9-ksu330` -> (`ksu`, `330`)."""
    match = VERSIONED_ID.match(payload_id)
    return (match.group("flavour"), match.group("version")) if match else None


def _sibling_daemon(daemon: str) -> tuple[str, str] | None:
    """(`ksud-<target>-kdp`, flavour id) for a flavour's own daemon, or None.

    The sibling is the pair the target is already served by: the same daemon name without the flavour
    that distinguishes the published file. It is where a created entry copies its device row from, and
    its presence is what says the target is real rather than a daemon name that is merely misspelled.
    None for the plain flavour, which has nothing to be created from, and for a shared hand-built pair
    (`ksud-s25u-kdp`), whose name is not a target's.
    """
    match = DAEMON.match(daemon)
    if match is None:
        return None
    suffix = match.group("suffix") or ""
    flavour = FLAVOURS.get(suffix)
    if not suffix or flavour is None:
        return None
    return f"ksud-{match.group('target')}-kdp", flavour


def _with_flavor(body: str, flavor: str) -> str:
    """The entry, declaring which project's KernelSU it serves.

    Written after the kernel versions, which is where the three entries the feed already carries for
    one target put it, and at the indentation of the fields around it - so a created entry reads like
    a written one and its diff is the lines that were added.
    """
    if re.search(r'"flavor"\s*:', body):
        return body
    match = re.search(r'\n(?P<indent>\s*)"kernelVersions"\s*:\s*\[', body)
    if not match:
        return body
    closing = body.index("]", match.end() - 1)
    return body[: closing + 1] + f',\n{match.group("indent")}"flavor": "{flavor}"' + body[closing + 1 :]


def _created_id_and_label(
    sibling: dict,
    flavor: str,
    version: str | None,
) -> tuple[str, str]:
    """The payload id and display name a new entry for [flavor] takes.

    The id follows the shape the feed's own trio already uses: the sibling's id without whatever
    release number it carries, then the flavour's prefix and this release's number. The label keeps the
    sibling's device text - which is the part a person is choosing between - and appends the flavour
    and the release, so a version retitle later has exactly one place to land.
    """
    if not version:
        raise SystemExit("a created entry needs --version: the id and the label both spell it out")
    code = version_code(version)
    if not code:
        raise SystemExit(f"cannot read a version code out of {version!r}")
    payload_id = sibling.get("payloadId", "")
    suffix = _suffix_of(payload_id)
    if suffix:
        # The whole suffix, prefix included: `pa3q-S938USQSCCZF9-ksu330` becomes
        # `pa3q-S938USQSCCZF9`, not `pa3q-S938USQSCCZF9-ksu`. Taking only the number leaves the flavour
        # the sibling was built as in the new id, and the feed would carry `-ksu-ksun340`.
        payload_id = payload_id[: -(len(suffix[0]) + len(suffix[1]) + 1)]
    display = sibling.get("displayName", "").strip()
    label = FLAVOUR_LABELS.get(flavor, flavor)
    return f"{payload_id}-{ID_PREFIXES[flavor]}{code}", f"{display} | {label} {version.lstrip('vV')} (test)"


def _created_entry(
    text: str,
    start: int,
    end: int,
    sibling: dict,
    payload_id: str,
    display: str,
    flavor: str,
    daemon: str,
    size: int,
    sha256: str,
    url_prefix: str | None,
    version: str | None,
) -> str:
    """The sibling's own text, pointed at the new pair.

    Built by rewriting a copy of the entry rather than from the fields this file happens to name. A
    device row carries things this tool has no business knowing about - `requiresFreshP0Session` - and a
    construction from known keys would drop whatever the next one is without saying so.
    """
    body = text[start:end]
    for key, value in (("payloadId", payload_id), ("displayName", display)):
        old = sibling.get(key, "")
        if f'"{key}": "{old}"' not in body:
            raise SystemExit(f"{sibling.get('payloadId')} does not spell its own {key} on one line")
        body = body.replace(f'"{key}": "{old}"', f'"{key}": "{value}"', 1)
    return _rewrite_entry(_with_flavor(body, flavor), sibling, daemon, size, sha256, url_prefix, version)


def _created(
    text: str,
    feed: str,
    daemon: str,
    size: int,
    sha256: str,
    version: str | None,
    url_prefix: str | None,
) -> tuple[str, list[dict]]:
    """[text] with an entry for [daemon] added, and the change to report.

    The sibling is looked up by daemon name, so the target is established by an entry that already
    serves it rather than by anything this run asserts about itself. Without one there is no device row
    to copy and no reason to believe the name is a target's, so it is refused instead of guessed.
    """
    derived = _sibling_daemon(daemon)
    if derived is None:
        raise SystemExit(
            f"{daemon} is not a flavour's own daemon, so there is nothing to create an entry from"
        )
    sibling_daemon, flavor = derived

    spans = _entry_spans(text)
    found = next(
        (
            span
            for span in spans
            if os.path.basename(span[2].get("kernelsu", {}).get("url", "")) == sibling_daemon
        ),
        None,
    )
    if found is None:
        raise SystemExit(
            f"no entry in {feed} serves {sibling_daemon}, so nothing describes the device "
            f"{daemon} would be offered to"
        )

    start, end, sibling = found
    payload_id, display = _created_id_and_label(sibling, flavor, version)
    if any(entry.get("payloadId") == payload_id for _s, _e, entry in spans):
        raise SystemExit(f"{feed} already has an entry with the id {payload_id}")

    body = _created_entry(
        text, start, end, sibling, payload_id, display, flavor, daemon, size, sha256, url_prefix, version
    )

    # The indentation of the entry being copied, so the new one sits in the array like a written one.
    # The sibling's own line is whitespace by construction; a file that is not indented falls back.
    line = text.rfind("\n", 0, start) + 1
    indent = text[line:start]
    if indent.strip():
        indent = "    "

    updated = text[:end] + ",\n" + indent + body + text[end:]
    return updated, [
        {
            "before": {"payloadId": payload_id, "displayName": display, "url": "(new entry)", "size": size},
            "after": {
                "payloadId": payload_id,
                "displayName": display,
                "url": f"{url_prefix.rstrip('/')}/kernelsu/{daemon}" if url_prefix else daemon,
                "size": size,
            },
            "createdFrom": sibling.get("payloadId", ""),
        }
    ]


def _version_text(code: str) -> str | None:
    """`330` -> `3.3.0`, for the display name that spells the version out."""
    if len(code) < 2:
        return None
    digits = code if len(code) >= 3 else code + "0"
    major, minor, patch = digits[-3], digits[-2], digits[-1]
    return f"{int(digits[:-2])}.{minor}.{patch}" if len(digits) > 3 else f"{major}.{minor}.{patch}"


def _retitled(display: str, old_text: str | None, version: str) -> str:
    """`... ReSukiSU 4.2.0-rc2 (test)` -> `... ReSukiSU 4.2.0-rc3 (test)`.

    The version a display name spells out is replaced whole, pre-release suffix included. The
    numeric part is what the payload id already carries, and a pre-release moves without moving it -
    `4.2.0-rc2` and `4.2.0-rc3` both derive `420`, so an id that stayed put while the label only
    followed the id would keep naming the build it replaced. `4.2.0` is a prefix of both, so a plain
    substring replace would leave the old suffix behind on the new one.
    """
    wanted = version.lstrip("vV")
    if not old_text or old_text not in display or wanted == old_text:
        return display
    pattern = re.escape(old_text) + r"(?:[-.][0-9A-Za-z.]+)?"
    return re.sub(pattern, lambda _: wanted, display, count=1)


def _digest(path: str) -> tuple[int, str]:
    import hashlib

    size = os.path.getsize(path)
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return size, digest.hexdigest()


def _entry_spans(text: str) -> list[tuple[int, int, dict]]:
    """Every entry in the file, with the byte range it occupies.

    The file is edited as text rather than re-serialised. A round trip through `json.dumps`
    reformats it - short arrays get exploded onto their own lines - and a feed that is
    installed from deserves a commit whose diff shows the two lines that changed.
    """
    decoder = json.JSONDecoder()
    position = text.index("[", text.index('"payloads"')) + 1
    spans: list[tuple[int, int, dict]] = []
    while True:
        while text[position] in " \t\r\n,":
            position += 1
        if text[position] == "]":
            return spans
        entry, end = decoder.raw_decode(text, position)
        spans.append((position, end, entry))
        position = end


def _with_version(entry: str, version: str) -> str:
    """The entry, with its `kernelsu` block declaring `version`.

    Sliced from the `"kernelsu"` key rather than from the entry's start: the exploit's object is the
    one that closes first, so taking the first brace in the entry writes the version into the wrong
    block - which is a mistake this function exists to make once instead of twice.
    """
    start = entry.index('"kernelsu"')
    block, rest = entry[start:].split("}", 1)
    return entry[:start] + _set_version(block + "}", version) + rest


def _set_version(block: str, version: str) -> str:
    """Puts `"version"` into a `kernelsu` block, or points an existing one at this release.

    `block` starts at the `"kernelsu"` key, so the object's own closing brace is the first one in it.
    The new field is written on its own line at the indentation of the fields above it, so the diff of
    a release is the lines that changed and not a reformatted entry.
    """
    closing = block.index("}")
    head, rest = block[:closing], block[closing:]
    if re.search(r'"version"\s*:', head):
        return re.sub(r'("version"\s*:\s*")[^"]*(")', r"\g<1>" + version + r"\g<2>", head, count=1) + rest
    fields = list(re.finditer(r'\n(\s+)"(?:url|size|sha256)"\s*:', head))
    if not fields:
        return block
    indent = fields[-1].group(1)
    body = head.rstrip()
    trailing = head[len(body):]
    if body.endswith(","):
        body = body[:-1]
    return body + f',\n{indent}"version": "{version}"' + trailing + rest


def _rewrite_entry(
    body: str,
    entry: dict,
    daemon: str,
    size: int,
    sha256: str,
    url_prefix: str | None,
    version: str | None,
) -> str:
    """One entry, with only the values that changed replaced."""
    artifact = entry["kernelsu"]
    old_url = artifact["url"]
    names = [old_url.rsplit("/", 1)[-1], daemon]

    if url_prefix:
        new_url = f"{url_prefix.rstrip('/')}/kernelsu/{daemon}"
    else:
        new_url = old_url.replace(names[0], daemon)

    # The artifact block, so a size or digest belonging to the exploit is never touched.
    block = body.index('"kernelsu"')
    head, tail = body[:block], body[block:]
    tail = tail.replace(f'"url": "{old_url}"', f'"url": "{new_url}"', 1)
    tail = re.sub(r'("size":\s*)' + str(artifact["size"]), r"\g<1>" + str(size), tail, count=1)
    if '"sha256"' in tail:
        tail = re.sub(r'("sha256":\s*")[0-9a-f]*(")', r"\g<1>" + sha256 + r"\g<2>", tail, count=1)
    else:
        # Written on its own line at the same indentation as the size it follows. The size carries
        # a comma only when something comes after it, and the digest is written last here, so both
        # shapes are handled rather than assumed.
        digest = tail
        match = re.search(r'(?P<line>\n(?P<indent>\s*)"size":\s*' + str(size) + r')(?P<comma>,?)', digest)
        if match:
            indent = match.group("indent")
            line = match.group("line")
            if match.group("comma"):
                digest = digest.replace(
                    line + ",",
                    line + f',\n{indent}"sha256": "{sha256}",',
                    1,
                )
            else:
                digest = digest.replace(
                    line,
                    line + f',\n{indent}"sha256": "{sha256}"',
                    1,
                )
        tail = digest
    if version:
        # `tail` starts at the `"kernelsu"` key, which is what _with_version slices from.
        tail = _with_version(tail, version.lstrip("vV"))
    return head + tail


def apply(
    repo: str,
    feed: str,
    daemon: str,
    size: int,
    sha256: str,
    version: str | None,
    payload_ids: list[str],
    migrate: bool,
    dry_run: bool,
    url_prefix: str | None = None,
    create: bool = False,
) -> list[dict]:
    path = os.path.join(repo, feed)
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    manifest = json.loads(text)

    targets = set(payload_ids)
    changes: list[dict] = []
    rebuilt: list[str] = []
    cursor = 0

    for start, end, entry in _entry_spans(text):
        artifact = entry.get("kernelsu")
        if not isinstance(artifact, dict):
            continue
        url = artifact.get("url", "")
        current = os.path.basename(url)

        republish = current == daemon
        # Moving off a shared pair: only where the entry is named for the build it serves,
        # because that is the only reason to believe the derived release belongs to it.
        if not republish and not (migrate and entry.get("payloadId") in targets):
            continue

        body = _rewrite_entry(text[start:end], entry, daemon, size, sha256, url_prefix, version)
        payload_id = entry.get("payloadId", "")
        after_id = payload_id
        display = entry.get("displayName", "")
        after_display = display

        suffix = _suffix_of(payload_id)
        if suffix and version:
            _flavour, old_code = suffix
            new_code = version_code(version)
            if new_code and new_code != old_code:
                after_id = f"{payload_id[: -len(old_code)]}{new_code}"
                body = body.replace(f'"payloadId": "{payload_id}"', f'"payloadId": "{after_id}"', 1)
            # The display name is retitled on its own, not as part of the id move: the two only
            # travel together for releases whose numeric code changed, and a pre-release pair
            # rebuilt onto a new candidate is exactly the case where the id must stay and the
            # label must not.
            after_display = _retitled(display, _version_text(old_code), version)
            if after_display != display:
                body = body.replace(f'"displayName": "{display}"', f'"displayName": "{after_display}"', 1)

        rebuilt.append(text[cursor:start])
        rebuilt.append(body)
        cursor = end
        changes.append(
            {
                "before": {
                    "payloadId": payload_id,
                    "displayName": display,
                    "url": url,
                    "size": artifact.get("size"),
                },
                "after": {
                    "payloadId": after_id,
                    "displayName": after_display,
                    "url": f"{url_prefix.rstrip('/')}/kernelsu/{daemon}" if url_prefix else url.replace(current, daemon),
                    "size": size,
                },
            }
        )

    if not changes and not create:
        raise SystemExit(f"nothing in {feed} serves {daemon}")

    if changes:
        rebuilt.append(text[cursor:])
        updated = "".join(rebuilt)
    else:
        updated, changes = _created(text, feed, daemon, size, sha256, version, url_prefix)

    result = json.loads(updated)

    # Two entries for one model, kernel version and flavour both match a run, and only the first
    # is used - so a duplicate is a feed that cannot say which pair it means.
    seen: dict[tuple, str] = {}
    for entry in result.get("payloads", []):
        key = (
            entry.get("flavor", "kernelsu"),
            tuple(entry.get("models", [])),
            tuple(entry.get("kernelVersions", [])),
        )
        other = seen.get(key)
        if other is not None:
            raise SystemExit(f"{entry.get('payloadId')} and {other} would both match the same devices")
        seen[key] = entry.get("payloadId", "")

    # Every artifact the app will ask for has to be reachable, and a relayed value is the one
    # mistake that would only show up as a failed run on a user's phone.
    for entry in result.get("payloads", []):
        artifact = entry["kernelsu"]
        if os.path.basename(artifact["url"]) != daemon:
            continue
        if artifact["size"] != size:
            raise SystemExit(f"{entry.get('payloadId')} still declares size {artifact['size']}")
        # The version is what the app offers a manager from, so an entry serving this pair while
        # naming another release is the same class of mistake as a relayed size.
        if version and artifact.get("version") != version.lstrip("vV"):
            raise SystemExit(
                f"{entry.get('payloadId')} declares KernelSU {artifact.get('version')!r}, "
                f"not {version.lstrip('vV')!r}"
            )

    if not dry_run:
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(updated)

    return changes


SELF_TEST_ENTRY = """    {
      "payloadId": "pa3q-S938USQSCCZF9-ksun340",
      "displayName": "Galaxy S25 Ultra | KernelSU-Next 3.4.0 (test)",
      "models": ["SM-S938U1"],
      "kernelVersions": ["6.6.98"],
      "flavor": "kernelsu-next",
      "exploit": {
        "url": "https://raw.githubusercontent.com/example/artifacts/pa3q/cve.so",
        "size": 104128
      },
      "kernelsu": {
        "url": "https://raw.githubusercontent.com/example/kernelsu/ksud-next-kdp",
        "size": 4227792,
        "sha256": "31beb817deb8e4b0945ca23d47a79fd4967984dde05ce914bd4c47d29553c9c0"
      }
    }"""


# One entry of the real feed's shape, including the device field that is nobody's business but this
# device's - which is the point of copying the sibling rather than rebuilding it from known keys.
SELF_TEST_FEED = """{
  "schemaVersion": 3,
  "payloads": [
    {
      "payloadId": "dm3q-S918BXXSAFZF5",
      "displayName": "Galaxy S23 Ultra SM-S918B | Kernel 5.15.189 (S918BXXSAFZF5)",
      "models": [
        "SM-S918B"
      ],
      "kernelVersions": [
        "5.15.189"
      ],
      "requiresFreshP0Session": true,
      "exploit": {
        "url": "https://raw.githubusercontent.com/example/artifacts/dm3q/cve.so",
        "size": 135752
      },
      "kernelsu": {
        "url": "https://raw.githubusercontent.com/example/kernelsu/ksud-dm3q-S918BXXSAFZF5-kdp",
        "size": 5115640,
        "sha256": "0698745d1e9295306548ae013d5ca7cdcb6374d9b60b543c0e65862818a21f2e",
        "version": "3.3.0"
      }
    }
  ]
}
"""


def self_test() -> int:
    """The version write, the naming it comes from, and the creation of a new flavour's entry.

    The case worth having here is the block: an entry's exploit object closes before the `kernelsu` one,
    so a version written from the entry's first brace lands on the exploit - which parses, reads as a
    harmless extra field, and would only be noticed by the app quietly ignoring it.

    Creation is checked against a feed on disk, because what it has to get right is a splice: the new
    entry beside the one it copies, valid JSON either side, the sibling untouched, and every field it
    did not name carried over.
    """
    failures = 0

    written = _with_version(SELF_TEST_ENTRY, "3.4.0")
    parsed = json.loads(written)
    if parsed.get("kernelsu", {}).get("version") != "3.4.0":
        print(f"  version landed outside the kernelsu block: {parsed.get('exploit')}")
        failures += 1
    if "version" in parsed.get("exploit", {}):
        print("  the exploit block was given a KernelSU version")
        failures += 1
    if parsed.get("kernelsu", {}).get("size") != 4227792:
        print("  the size beside it was rewritten")
        failures += 1
    # Rewriting an entry that already declares one points it at the new release rather than adding a
    # second field to an object that can only have one.
    if json.loads(_with_version(written, "3.5.0"))["kernelsu"]["version"] != "3.5.0":
        print("  an existing version was not replaced")
        failures += 1
    if written.count('"version"') != 1:
        print(f"  the version was written {written.count(chr(34) + 'version' + chr(34))} times")
        failures += 1

    for payload_id, expected in (
        ("pa3q-S938USQSCCZF9-ksun340", "3.4.0"),
        ("pa3q-S938USQSCCZF9-ksu330", "3.3.0"),
        ("dm1q-S911U1UES6DYI3", None),
        ("galaxy-s25-series-2026-06-07", None),
    ):
        suffix = _suffix_of(payload_id)
        actual = _version_text(suffix[1]) if suffix else None
        if actual != expected:
            print(f"  {payload_id}: expected {expected}, got {actual}")
            failures += 1

    # A pre-release moves the label without moving the payload id's number: `4.2.0-rc2` and
    # `4.2.0-rc3` both derive `420`, so this is the case the id comparison cannot see - and the one
    # that left a republished rc3 pair still named rc2.
    for display, old_text, version, expected in (
        (
            "Galaxy S25 Ultra | ReSukiSU 4.2.0-rc2 (test)",
            "4.2.0",
            "v4.2.0-rc3",
            "Galaxy S25 Ultra | ReSukiSU 4.2.0-rc3 (test)",
        ),
        (
            "Galaxy S25 Ultra | KernelSU 3.3.0 (test)",
            "3.3.0",
            "v3.4.0",
            "Galaxy S25 Ultra | KernelSU 3.4.0 (test)",
        ),
        (
            "Galaxy S25 Ultra | KernelSU-Next 3.4.0 (test)",
            "3.4.0",
            "v3.4.0",
            "Galaxy S25 Ultra | KernelSU-Next 3.4.0 (test)",
        ),
    ):
        actual = _retitled(display, old_text, version)
        if actual != expected:
            print(f"  retitle {display!r} -> {actual!r}, wanted {expected!r}")
            failures += 1

    # Where a created entry's device row comes from, and the two names that are not inherited.
    for daemon, expected in (
        ("ksud-next-dm3q-S918BXXSAFZF5-kdp", ("ksud-dm3q-S918BXXSAFZF5-kdp", "kernelsu-next")),
        ("ksud-rsksu-pa3q-S938USQSCCZF9-kdp", ("ksud-pa3q-S938USQSCCZF9-kdp", "resukisu")),
        ("ksud-pa3q-S938USQSCCZF9-kdp", None),
        ("ksud-s25u-kdp", None),
        ("ksud-next-s25u-kdp", ("ksud-s25u-kdp", "kernelsu-next")),
    ):
        actual = _sibling_daemon(daemon)
        if actual != expected:
            print(f"  sibling of {daemon}: expected {expected}, got {actual}")
            failures += 1

    for sibling_id, display, flavor, version, expected_id in (
        # The two shapes a sibling comes in: an id that already carries a flavour and a release, and
        # one that carries neither. Both have to yield the flavour being added, not the one before it.
        ("dm3q-S918BXXSAFZF5", "Galaxy S23", "kernelsu-next", "v3.4.0", "dm3q-S918BXXSAFZF5-ksun340"),
        ("pa3q-S938USQSCCZF9-ksu330", "Galaxy S25", "kernelsu-next", "v3.4.0", "pa3q-S938USQSCCZF9-ksun340"),
        ("pa3q-S938USQSCCZF9-ksu330", "Galaxy S25", "resukisu", "v4.2.0-rc3", "pa3q-S938USQSCCZF9-rsksu420"),
    ):
        actual, label = _created_id_and_label(
            {"payloadId": sibling_id, "displayName": display}, flavor, version
        )
        if actual != expected_id:
            print(f"  created id: expected {expected_id}, got {actual}")
            failures += 1
        if version.lstrip("vV") not in label or "|" not in label:
            print(f"  created label does not name the release beside the device: {label!r}")
            failures += 1
    # A flavour's label has to be the project's own name, or the sheet offers one project under
    # another's - and a created entry is the only place the label is written from the flavour id.
    for flavor in ("kernelsu", "kernelsu-next", "resukisu"):
        if flavor not in FLAVOUR_LABELS or flavor not in ID_PREFIXES:
            print(f"  no label or id prefix for {flavor}")
            failures += 1

    # Flavour written once, after the kernel versions, and not again on a second pass.
    flavored = _with_flavor(SELF_TEST_FEED, "kernelsu-next")
    if json.loads(flavored)["payloads"][0].get("flavor") != "kernelsu-next":
        print("  the flavor field did not land in the entry")
        failures += 1
    if flavored != _with_flavor(flavored, "resukisu"):
        print("  a second flavor was written onto an entry that already declared one")
        failures += 1

    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "feed.json")
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(SELF_TEST_FEED)
        try:
            apply(
                repo=directory,
                feed="feed.json",
                daemon="ksud-next-dm3q-S918BXXSAFZF5-kdp",
                size=4230992,
                sha256="ab" * 32,
                version="v3.4.0",
                payload_ids=[],
                migrate=False,
                dry_run=False,
                url_prefix="https://raw.githubusercontent.com/example/payloads/main",
                create=True,
            )
        except SystemExit as error:
            print(f"  creating an entry was refused: {error}")
            failures += 1

        with open(path, encoding="utf-8") as handle:
            after = json.load(handle)["payloads"]
        if len(after) != 2:
            print(f"  expected the entry beside the one it copies, got {len(after)} entries")
            failures += 1
        else:
            new, old = after[1], after[0]
            checks = (
                (new["payloadId"] == "dm3q-S918BXXSAFZF5-ksun340", "the new id"),
                (new.get("flavor") == "kernelsu-next", "the flavor"),
                (new.get("requiresFreshP0Session") is True, "the device field it never named"),
                (new["models"] == old["models"], "the models"),
                (new["kernelVersions"] == old["kernelVersions"], "the kernel versions"),
                (new["exploit"] == old["exploit"], "the exploit the pair shares"),
                (new["kernelsu"]["size"] == 4230992, "the size it was published with"),
                (new["kernelsu"]["sha256"] == "ab" * 32, "the digest it was published with"),
                (new["kernelsu"]["version"] == "3.4.0", "the release it was built from"),
                (os.path.basename(new["kernelsu"]["url"]) == "ksud-next-dm3q-S918BXXSAFZF5-kdp", "the daemon"),
                # The sibling is the entry devices are running today, so a creation that touched it
                # would be a flavour added by taking the other one away.
                (old["kernelsu"] == {
                    "url": "https://raw.githubusercontent.com/example/kernelsu/ksud-dm3q-S918BXXSAFZF5-kdp",
                    "size": 5115640,
                    "sha256": "0698745d1e9295306548ae013d5ca7cdcb6374d9b60b543c0e65862818a21f2e",
                    "version": "3.3.0",
                }, "the sibling left alone"),
            )
            for passed, what in checks:
                if not passed:
                    print(f"  the created entry got {what} wrong")
                    failures += 1

        # And a daemon whose target is not in the feed at all is refused rather than invented.
        try:
            _created(
                SELF_TEST_FEED,
                "feed.json",
                "ksud-next-e9z-S999ZZZ9ZZZ-kdp",
                1,
                "ab" * 32,
                "v3.4.0",
                None,
            )
        except SystemExit:
            pass
        else:
            print("  a creation was allowed with no entry describing the device")
            failures += 1

    print(f"self-test: 1 entry shape, 4 payload ids, 3 retitles, 5 siblings, 1 creation, {failures} failure(s)")
    return 1 if failures else 0


def backfill(repo: str, feed: str, dry_run: bool) -> list[dict]:
    """Writes the version every entry already spells into its payload id.

    The field is new, and an entry is only rewritten when the pair it serves is rebuilt - so a feed that
    gained the field would keep serving `pa3q-...-ksun340` without saying 3.4.0 anywhere the app reads it.
    The id is where that fact already is: this same tool wrote it from the tag the pair was built from,
    which is why the two agreeing is the check here rather than a guess.

    An entry whose id carries no version is left alone, and an entry that declares a different version
    than its id is refused: the two are written from one fact, so a disagreement means one of them was
    edited by hand and the app would be offered a manager for a KernelSU nothing else names.
    """
    path = os.path.join(repo, feed)
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    rebuilt: list[str] = []
    changes: list[dict] = []
    cursor = 0

    for start, end, entry in _entry_spans(text):
        artifact = entry.get("kernelsu")
        suffix = _suffix_of(entry.get("payloadId", ""))
        if not isinstance(artifact, dict) or not suffix:
            continue
        version = _version_text(suffix[1])
        if not version:
            continue
        declared = artifact.get("version")
        if declared == version:
            continue
        if declared:
            raise SystemExit(
                f"{entry.get('payloadId')} declares {declared} while its id says {version}"
            )
        body = _with_version(text[start:end], version)
        rebuilt.append(text[cursor:start])
        rebuilt.append(body)
        cursor = end
        changes.append(
            {
                "before": {"payloadId": entry.get("payloadId"), "version": declared},
                "after": {"payloadId": entry.get("payloadId"), "displayName": entry.get("displayName"), "version": version},
            }
        )

    if changes and not dry_run:
        rebuilt.append(text[cursor:])
        updated = "".join(rebuilt)
        json.loads(updated)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(updated)

    return changes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=".", help="payload repository root")
    parser.add_argument("--feed", default="support/targets-v3.json")
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="write kernelsu.version for entries whose payload id already carries one",
    )
    parser.add_argument("--self-test", action="store_true", help="check the version write and stop")
    parser.add_argument("--daemon", help="file name of the daemon that was built")
    parser.add_argument("--artifact", help="path to the built daemon, to read its size and digest")
    parser.add_argument("--size", type=int)
    parser.add_argument("--sha256")
    parser.add_argument("--version", help="the KernelSU tag the pair was built from, e.g. v3.4.0")
    parser.add_argument("--payload-id", action="append", default=[], help="entries to migrate")
    parser.add_argument("--migrate", action="store_true", help="move named entries onto a new daemon")
    parser.add_argument(
        "--create",
        action="store_true",
        help="add an entry when the feed has none serving --daemon, copying the target's own entry",
    )
    parser.add_argument("--url-prefix", help="repository raw-URL prefix this run publishes under")
    parser.add_argument("--dry-run", action="store_true")
    arguments = parser.parse_args()

    if arguments.self_test:
        return self_test()

    if arguments.backfill:
        changes = backfill(repo=arguments.repo, feed=arguments.feed, dry_run=arguments.dry_run)
        for change in changes:
            print(f"{change['before']['payloadId']}: version {change['after']['version']}")
        print(
            f"{len(changes)} feed entr{'y' if len(changes) == 1 else 'ies'} "
            f"given a version{' (dry run)' if arguments.dry_run else ''}"
        )
        return 0

    if not arguments.daemon:
        parser.error("give --daemon, or --backfill")

    size, sha256 = arguments.size, arguments.sha256
    if arguments.artifact:
        size, sha256 = _digest(arguments.artifact)
    if size is None or sha256 is None:
        parser.error("give --artifact, or both --size and --sha256")

    changes = apply(
        repo=arguments.repo,
        feed=arguments.feed,
        daemon=arguments.daemon,
        size=size,
        sha256=sha256,
        version=arguments.version,
        payload_ids=arguments.payload_id,
        migrate=arguments.migrate,
        dry_run=arguments.dry_run,
        url_prefix=arguments.url_prefix,
        create=arguments.create,
    )

    added = 0
    for change in changes:
        before, after = change["before"], change["after"]
        source = change.get("createdFrom")
        if source:
            added += 1
            print(f"{after['payloadId']} added, from {source}")
            print(f"  url  {after['url']}")
            print(f"  size {after['size']}")
            print(f"  name {after['displayName']}")
            continue
        print(f"{before['payloadId']} -> {after['payloadId']}")
        print(f"  url  {before['url']}")
        print(f"   ->  {after['url']}")
        print(f"  size {before['size']} -> {after['size']}")
        if before["displayName"] != after["displayName"]:
            print(f"  name {after['displayName']}")
    what = "added" if added == len(changes) else "updated"
    print(f"{len(changes)} feed entr{'y' if len(changes) == 1 else 'ies'} {what}{' (dry run)' if arguments.dry_run else ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
