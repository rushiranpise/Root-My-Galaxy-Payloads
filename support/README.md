# Support feed schema

`targets-v3.json` keeps one entry for each shared exploit and KernelSU payload.
Automatic selection matches the exact device model and three-part kernel
version, such as `6.6.98`.

Each entry contains only:

- `payloadId` and `displayName`;
- one or more exact `Build.MODEL` values in `models`;
- one or more versions in `kernelVersions`;
- `url` and `size` for the exploit and KernelSU artifacts.

A `sha256` may also be declared on an artifact, which the client verifies in place of the size.
Pairs rebuilt by CI declare one, because the build computes the digest of what it just produced;
entries written before that was the case carry only a size, which the client still checks.

The `kernelsu` artifact may also declare `version`: the KernelSU release the daemon beside it was
built from, such as `3.4.0`. A pair rebuilt by CI declares one, taken from the tag the run built at
(`tools/update_feed.py --version`), and `tools/update_feed.py --backfill` writes it for entries whose
`payloadId` already carries a version and which are not being rebuilt. The app uses it to offer the
manager of the KernelSU a run will actually stage - the daemon and the manager have to be the same
release - and falls back to the flavour's own release for an entry that declares none.

An entry may also declare `flavor`: which project's KernelSU its `kernelsu` artifact is, one of
`kernelsu`, `kernelsu-next` or `resukisu`. The app compares it against the three projects it knows
and offers that flavour's manager; an entry that declares none is the plain `kernelsu` one, which is
what every entry written before flavours existed is. One device can be served by several flavours at
once - the app shows them as a row of chips to choose between - so the two entries differ in this
field and in the artifact they name, and nothing else. `tools/update_feed.py --create` writes one
beside an entry that already serves the device, which is how a second flavour reaches a target that
has only ever been served the first.

An entry may additionally set `requiresFreshP0Session` to `true` when slide
discovery and exploitation must run in the same payload process. The app then
disables its per-boot P0 cache for that profile and gives the single combined
attempt the target-specific long timeout. The field defaults to `false`, so
existing profiles retain the cached multi-attempt behavior.

The app extracts the leading numeric version from `uname -r`. Kernel suffixes,
Android build displays, fingerprints, and security-patch dates do not
participate in matching.

`targets-v2.json` remains unchanged for released 0.2.3 clients. New clients
read only schema version 3.

## How entries are published

A KernelSU release makes every pair in this file stale, and `upstream-watch.yml` is what closes
that gap: it rebases the Samsung patch onto the new tag, builds a pair for each target the feed
serves, and commits the artifacts with the entry that points at them. The targets and the release
each pair is built for are derived by [`../tools/pairs.py`](../tools/pairs.py) from this file and
the artifacts already in [`../kernelsu/`](../kernelsu/) - a release is read out of the `vermagic`
of the module being replaced, so a rebuild claims exactly what the module users are running
claims, and nothing has to be typed in by hand.

A pair has two KMI names and they are not interchangeable. The artifact keeps the name the device's
modules already use, which for the 5.15 ports carries the kernel release (`android13-5.15.189`),
while the DDK publishes its images per family (`android13-5.15`) and never per release. Both are
derived from the same module name and passed on separately, and `ksu-build.yml` checks its image
exists before pulling it - a job's container is created before any of its steps run, so a tag that
was never published is otherwise a failed job about a missing manifest rather than about the KMI.

What that leaves for a person, and why:

- **A rebase that conflicts.** A KernelSU release that rewrites the code the Samsung delta
touches cannot be merged by a runner. The workflow tries, and files an issue with the conflicting
hunks when it cannot.
- **Entries served by a hand-built pair.** The `s25u` bundle and its siblings are one module
shared by several models, built outside the pair job. `tools/pairs.py` reports them instead of
guessing, and the run summary lists them.
- **A device port with no document.** The release a pair must claim is device truth; where it has
never been written down there is nothing to build against.
