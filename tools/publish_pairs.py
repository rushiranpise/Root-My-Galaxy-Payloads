#!/usr/bin/env python3
"""Publish a whole batch of built pairs in one commit.

`ksu-build.yml` publishes one pair per run, and for a dispatch that is right: one target, one
flavour, one commit, with the feed entry landing beside the artifact it names. A rebuild of many
pairs is not that shape - the watcher builds every pair the feed serves - and one commit per pair
means one `git push` per pair, so the runs race on the branch and the feed is left half updated
between two of them. This publishes the batch instead: every pair is checked and written into the
working tree, and the caller makes one commit of the result.

It is deliberately the same three checks the per-pair publish job runs, in the same order, against
the same artifacts:

1. `verify_pair.py`, on the module and the daemon as the build left them.
2. `check_pair_version.py`, on the receipts that travelled with them, so the two halves are held to
   one version and that version to the tag this run asked for.
3. `update_feed.py --create`, once the pair is in `kernelsu/`, so the entry and the artifact it
   names arrive in the same commit.

The checks run against the downloaded artifacts rather than copies, so a pair that fails leaves
nothing behind: the tree only ever holds pairs that passed.

Nothing is published from a pair the build did not leave both halves of. A build leg that failed
must not withhold the rest of the batch - that is the point of publishing the batch together - so
those pairs are reported and skipped, and the exit code says whether everything planned was
published.

Usage:
    python3 tools/publish_pairs.py --repo . --plan plan.json --downloaded artifacts \\
        --url-prefix https://raw.githubusercontent.com/OWNER/REPO/main [--commit] [--self-test]

Exit codes: 0 when every planned pair was published, 1 when some pair was not - the pairs that
were are still in the tree, and the caller commits them.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys

# The suffix every file this flavour publishes carries, so two flavours can serve one target.
SUFFIX = {"kernelsu": "", "kernelsu-next": "-next", "resukisu": "-rsksu"}


def suffix(flavor: str) -> str:
    if flavor not in SUFFIX:
        raise SystemExit(f"unknown flavour {flavor!r}")
    return SUFFIX[flavor]


def read_ref(receipt: str):
    """The KernelSU tag a half was built from, as its own receipt records it."""
    if not os.path.exists(receipt):
        return None
    with open(receipt, encoding="utf-8") as handle:
        for line in handle:
            if line.startswith("ref="):
                return line.strip()[len("ref="):]
    return None


def pick(directory: str, ext=None, exclude_ext=None):
    """The one file of this shape in a directory, or None.

    Read rather than named: the module's name carries the KMI, and for the 5.15 ports that is the
    kernel release (`android13-5.15.189_...`) while the image it was built in is the family. Taking
    the file the build left avoids re-deriving which of the two this pair's name uses.
    """
    if not os.path.isdir(directory):
        return None
    found = []
    for name in sorted(os.listdir(directory)):
        path = os.path.join(directory, name)
        if not os.path.isfile(path):
            continue
        if ext is not None and not name.endswith(ext):
            continue
        if exclude_ext is not None and name.endswith(exclude_ext):
            continue
        found.append(path)
    return found[0] if len(found) == 1 else None


def pair_paths(downloaded: str, pair):
    """Where a pair's two artifact directories are, and what is in them."""
    flavor, target = pair["flavor"], pair["targetId"]
    module_dir = os.path.join(downloaded, f"pair-module-{flavor}-{target}")
    daemon_dir = os.path.join(downloaded, f"pair-ksud-{flavor}-{target}")
    return {
        "module": pick(module_dir, ext=".ko"),
        "daemon": pick(daemon_dir, exclude_ext=".txt"),
        "module_receipt": os.path.join(module_dir, "module-version.txt"),
        "daemon_receipt": os.path.join(daemon_dir, "daemon-version.txt"),
    }


def last_line(result) -> str:
    text = (result.stderr or "").strip() or (result.stdout or "").strip()
    return text.splitlines()[-1] if text else f"exit {result.returncode}"


def publish_one(repo: str, downloaded: str, pair, url_prefix: str, run=subprocess.run):
    """Check one pair and write it into the tree. Returns (ok, note).

    The two build halves are copied in only after both checks pass, and removed again if the feed
    refuses the entry, so a pair that did not publish is never left in `kernelsu/` for the caller's
    commit to pick up.
    """
    flavor, target = pair["flavor"], pair["targetId"]
    paths = pair_paths(downloaded, pair)
    if not paths["module"] or not paths["daemon"]:
        return False, "the build left no complete pair"

    version = read_ref(paths["module_receipt"])
    if not version:
        return False, "the module receipt names no ref"

    def tool(*argv):
        return run([sys.executable, os.path.join(repo, "tools", argv[0]), *argv[1:]],
                   cwd=repo, capture_output=True, text=True)

    result = tool("verify_pair.py", "--module", paths["module"], "--daemon", paths["daemon"],
                  "--release", pair["release"])
    if result.returncode != 0:
        return False, f"verify_pair refused it: {last_line(result)}"

    result = tool("check_pair_version.py",
                  "--module-receipt", paths["module_receipt"],
                  "--daemon-receipt", paths["daemon_receipt"],
                  "--daemon-binary", paths["daemon"],
                  "--flavor", flavor, "--ref", pair["ksu_ref"])
    if result.returncode != 0:
        return False, f"check_pair_version refused it: {last_line(result)}"

    daemon_name = os.path.basename(paths["daemon"])
    module_name = os.path.basename(paths["module"])
    published = [os.path.join(repo, "kernelsu", module_name), os.path.join(repo, "kernelsu", daemon_name)]
    for source, destination in zip((paths["module"], paths["daemon"]), published):
        shutil.copy(source, destination)

    argv = ["update_feed.py", "--repo", ".", "--daemon", daemon_name,
            "--artifact", f"kernelsu/{daemon_name}", "--version", version, "--create",
            "--url-prefix", url_prefix]
    migrate = pair.get("migrate") or ""
    if migrate:
        argv.append("--migrate")
        for payload_id in migrate.split(","):
            argv += ["--payload-id", payload_id.strip()]
    result = tool(*argv)
    if result.returncode != 0:
        for path in published:
            os.remove(path)
        return False, f"update_feed refused the entry: {last_line(result)}"

    note = f"{flavor} {target}" if not migrate else f"{flavor} {target} (migrating {migrate})"
    return True, f"{note} -> {version}"


def commit(repo: str, published, run=subprocess.run):
    """One commit for the batch, and none at all when it published nothing new."""
    if not published:
        return False
    # The receipts are build evidence for the checks above, not artifacts: the app reads the daemon
    # and the feed, and nothing reads these. Removed before anything is staged, as the per-pair
    # publish job does, so they cannot be committed by accident. The batch never writes one, but the
    # tree it stages is not only what it wrote.
    kernelsu = os.path.join(repo, "kernelsu")
    if os.path.isdir(kernelsu):
        for name in os.listdir(kernelsu):
            if name.endswith("-version.txt"):
                os.remove(os.path.join(kernelsu, name))
    files = ["kernelsu", "support/targets-v3.json"]
    run(["git", "add", *files], cwd=repo, check=True)
    # Staged rather than assumed: a pair the feed already served leaves no change behind, and a
    # batch of those would otherwise be an empty commit.
    staged = run(["git", "diff", "--cached", "--quiet"], cwd=repo)
    if staged.returncode == 0:
        return False
    lines = "\n".join(f"{flavor} {target}: {version}" for flavor, target, version in published)
    message = (f"build(payloads): publish {len(published)} pair(s) from the rebuild\n\n"
               f"{lines}\n\nOne commit for the whole batch, so the feed is never left half updated\n"
               "between two pushes to the branch it is served from.")
    run(["git", "commit", "-q", "-m", message], cwd=repo, check=True)
    return True


def self_test() -> int:
    """The batching decisions, against a fake runner and a fake pair tree."""
    import tempfile

    failures = []
    calls = []

    class Result:
        def __init__(self, returncode):
            self.returncode = returncode
            self.stdout = ""
            self.stderr = "refused" if returncode else ""

    def tool_name(argv):
        """Which tool a recorded argv invoked: the runner is python, the tool is its argv[1]."""
        for arg in argv:
            if arg.endswith(".py"):
                return os.path.basename(arg)
        return os.path.basename(argv[0])

    def recording(vegetables=()):
        def run(argv, **kwargs):
            calls.append(argv)
            if argv[0] == "git":
                # `git diff --cached --quiet` exits 1 when something is staged, which is the case
                # this batch is always in.
                return Result(1 if argv[1] == "diff" else 0)
            return Result(1 if tool_name(argv) in vegetables else 0)
        return run

    claims = []

    def check(claim, condition):
        claims.append(claim)
        if not condition:
            failures.append(claim)

    with tempfile.TemporaryDirectory() as root:
        repo = os.path.join(root, "repo")
        downloaded = os.path.join(root, "downloaded")
        os.makedirs(os.path.join(repo, "kernelsu"))
        os.makedirs(os.path.join(repo, "support"))
        def make_pair(flavor, target, kmi="android15-6.6", ref="v3.4.0"):
            """What the build leaves for one pair, in the layout the artifacts are unpacked into."""
            suffix_ = SUFFIX[flavor]
            module_dir = os.path.join(downloaded, f"pair-module-{flavor}-{target}")
            daemon_dir = os.path.join(downloaded, f"pair-ksud-{flavor}-{target}")
            os.makedirs(module_dir)
            os.makedirs(daemon_dir)
            with open(os.path.join(module_dir, f"{kmi}_kernelsu{suffix_}-{target}-kdp.ko"), "wb") as handle:
                handle.write(b"module")
            with open(os.path.join(daemon_dir, f"ksud{suffix_}-{target}-kdp"), "wb") as handle:
                handle.write(b"daemon")
            with open(os.path.join(module_dir, "module-version.txt"), "w") as handle:
                handle.write(f"version=33294\nref={ref}\n")
            with open(os.path.join(daemon_dir, "daemon-version.txt"), "w") as handle:
                handle.write(f"version=33294\nref={ref}\n")

        pair = {"flavor": "kernelsu-next", "targetId": "A1", "release": "6.6.1-x", "ksu_ref": "v3.4.0"}
        make_pair("kernelsu-next", "A1")

        ok, note = publish_one(repo, downloaded, pair, "https://example/raw", run=recording())
        check("a complete pair publishes", ok and "A1" in note and "v3.4.0" in note)
        check("the module lands in kernelsu/",
              os.path.exists(os.path.join(repo, "kernelsu", "android15-6.6_kernelsu-next-A1-kdp.ko")))
        check("the daemon lands in kernelsu/",
              os.path.exists(os.path.join(repo, "kernelsu", "ksud-next-A1-kdp")))
        feed = next(argv for argv in calls if tool_name(argv) == "update_feed.py")
        check("the entry is named for the receipt's ref, not the plan's", "v3.4.0" in feed)
        check("it creates the entry", "--create" in feed)

        # A flavour whose suffix is empty is still named as the feed serves it.
        calls.clear()
        plain = dict(pair, flavor="kernelsu", targetId="A2")
        make_pair("kernelsu", "A2")
        check("the plain flavour's suffix is empty", suffix("kernelsu") == "")
        ok, _ = publish_one(repo, downloaded, plain, "https://example/raw", run=recording())
        check("kernelsu publishes through the same path, with no suffix",
              ok and os.path.exists(os.path.join(repo, "kernelsu", "ksud-A2-kdp")))

        # A half the build did not leave: skipped, not fatal, and nothing written.
        missing = dict(pair, targetId="A3")
        ok, note = publish_one(repo, downloaded, missing, "https://example/raw", run=recording())
        check("an incomplete pair is skipped", not ok and "no complete pair" in note)
        check("and leaves nothing in kernelsu/",
              not any("A3" in name for name in os.listdir(os.path.join(repo, "kernelsu"))))

        # A check that refuses: the pair must not be copied in, so the commit cannot pick it up.
        make_pair("kernelsu-next", "A4")
        before = sorted(os.listdir(os.path.join(repo, "kernelsu")))
        ok, note = publish_one(repo, downloaded, dict(pair, targetId="A4"), "https://example/raw",
                               run=recording(vegetables=("check_pair_version.py",)))
        check("a refused pair is not published", not ok and "check_pair_version" in note)
        check("and is not copied into kernelsu/", sorted(os.listdir(os.path.join(repo, "kernelsu"))) == before)

        # The feed refusing the entry takes the pair back out again.
        make_pair("kernelsu-next", "A5")
        ok, note = publish_one(repo, downloaded, dict(pair, targetId="A5"), "https://example/raw",
                               run=recording(vegetables=("update_feed.py",)))
        check("a refused entry is reported", not ok and "update_feed" in note)
        check("and the pair is withdrawn",
              not os.path.exists(os.path.join(repo, "kernelsu", "ksud-next-A5-kdp")))
        # The migration a pair carries has to reach the feed step, not just the plan.
        calls.clear()
        ok, _ = publish_one(repo, downloaded, dict(pair, targetId="A1", migrate="x,y"),
                            "https://example/raw", run=recording())
        migrate_call = next(argv for argv in calls if tool_name(argv) == "update_feed.py")
        check("a pair's migrations reach update_feed", "--migrate" in migrate_call and "x" in migrate_call)

        # One commit for the batch, listing what it published.
        calls.clear()
        stray = os.path.join(repo, "kernelsu", "module-version.txt")
        with open(stray, "w") as handle:
            handle.write("version=1\nref=v3.4.0\n")
        made = commit(repo, [("kernelsu-next", "A1", "v3.4.0"), ("resukisu", "A2", "v4.2.0-rc3")],
                      run=recording())
        check("a build receipt is not committed with the batch", not os.path.exists(stray))
        check("the batch commits once", made)
        message = next(argv for argv in calls if argv[0] == "git" and argv[1] == "commit")
        check("the message counts the pairs", "publish 2 pair(s)" in message[-1])
        check("and names each of them",
              "kernelsu-next A1: v3.4.0" in message[-1] and "resukisu A2: v4.2.0-rc3" in message[-1])
        check("nothing is published without a run to publish from",
              not commit(repo, [], run=recording()))

        check("the suffix table covers every flavour", set(SUFFIX) == {"kernelsu", "kernelsu-next", "resukisu"})

    if failures:
        print("self-test: FAIL")
        for claim in failures:
            print(f"  not true: {claim}")
        return 1
    print(f"self-test: {len(claims)} batching claims hold")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=".", help="payload repository root")
    parser.add_argument("--plan", help="JSON list of pairs, as `pairs.py --matrix` derives them")
    parser.add_argument("--downloaded", help="directory the run's pair artifacts were unpacked into")
    parser.add_argument("--url-prefix", help="repository raw-URL prefix this run publishes under")
    parser.add_argument("--commit", action="store_true", help="make the batch's single commit")
    parser.add_argument("--self-test", action="store_true", help="check the batching decisions and stop")
    args = parser.parse_args()

    if args.self_test:
        return self_test()

    for required in ("plan", "downloaded", "url_prefix"):
        if not getattr(args, required):
            parser.error(f"--{required.replace('_', '-')} is required unless --self-test")

    with open(args.plan, encoding="utf-8") as handle:
        plan = json.load(handle)

    published, refused = [], []
    for pair in plan:
        ok, note = publish_one(args.repo, args.downloaded, pair, args.url_prefix)
        if ok:
            published.append((pair["flavor"], pair["targetId"], note.rsplit(" -> ", 1)[-1]))
            print(f"  published {note}")
        else:
            refused.append((pair["flavor"], pair["targetId"], note))
            print(f"  NOT published {pair['flavor']} {pair['targetId']}: {note}")

    print(f"\npublished {len(published)} of {len(plan)}")
    if args.commit and published:
        if commit(args.repo, published):
            print("committed the batch")
        else:
            print("nothing changed; the feed already serves every pair that published")
    elif published:
        print("not committing (--commit was not asked for)")

    return 1 if refused else 0


if __name__ == "__main__":
    raise SystemExit(main())
