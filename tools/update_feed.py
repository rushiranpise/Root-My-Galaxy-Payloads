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

from pairs import VERSIONED_ID, version_code  # noqa: E402  (the tools directory is the module path)


def _suffix_of(payload_id: str) -> tuple[str, str] | None:
    """`pa3q-S938USQSCCZF9-ksu330` -> (`ksu`, `330`)."""
    match = VERSIONED_ID.match(payload_id)
    return (match.group("flavour"), match.group("version")) if match else None


def _version_text(code: str) -> str | None:
    """`330` -> `3.3.0`, for the display name that spells the version out."""
    if len(code) < 2:
        return None
    digits = code if len(code) >= 3 else code + "0"
    major, minor, patch = digits[-3], digits[-2], digits[-1]
    return f"{int(digits[:-2])}.{minor}.{patch}" if len(digits) > 3 else f"{major}.{minor}.{patch}"


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
                old_text = _version_text(old_code)
                if old_text and old_text in display:
                    after_display = display.replace(old_text, version.lstrip("vV"))
                    body = body.replace(f'"displayName": "{display}"', f'"displayName": "{after_display}"', 1)

        rebuilt.append(text[cursor:start])
        rebuilt.append(body)
        cursor = end
        changes.append(
            {
                "before": {"payloadId": payload_id, "url": url, "size": artifact.get("size")},
                "after": {
                    "payloadId": after_id,
                    "displayName": after_display,
                    "url": f"{url_prefix.rstrip('/')}/kernelsu/{daemon}" if url_prefix else url.replace(current, daemon),
                    "size": size,
                },
            }
        )

    if not changes:
        raise SystemExit(f"nothing in {feed} serves {daemon}")

    rebuilt.append(text[cursor:])
    updated = "".join(rebuilt)
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


def self_test() -> int:
    """The version write and the naming it comes from, checked without a feed or a rebuild.

    The case worth having here is the block: an entry's exploit object closes before the `kernelsu` one,
    so a version written from the entry's first brace lands on the exploit - which parses, reads as a
    harmless extra field, and would only be noticed by the app quietly ignoring it.
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

    print(f"self-test: 1 entry shape, 4 payload ids, {failures} failure(s)")
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
    )

    for change in changes:
        before, after = change["before"], change["after"]
        print(f"{before['payloadId']} -> {after['payloadId']}")
        print(f"  url  {before['url']}")
        print(f"   ->  {after['url']}")
        print(f"  size {before['size']} -> {after['size']}")
        if before["payloadId"] != after["payloadId"]:
            print(f"  name {after['displayName']}")
    print(f"{len(changes)} feed entr{'y' if len(changes) == 1 else 'ies'} updated{' (dry run)' if arguments.dry_run else ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
