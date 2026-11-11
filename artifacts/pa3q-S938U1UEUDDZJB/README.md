# S938U1UEUDDZJB CVE-2026-98260 payload

This is the DDZJB timer payload. It is built from the timer engine in
`src/targets/pa3q-S938U1UEUDDZJB` and embeds the exact
`6.6.127-android15-8-p87b1ce1-abogkiS938USQUDDZJB-4k` profile.

- `cve-2026-98260-app.so`: the app payload. Its constructor calibrates KASLR
  and the linear-map base, runs the timer UAF, and starts the app helper daemon
  with root so the normal `--late-load` path still works.

The three feed rows point at the existing 6.6 KDP daemons as test daemons until
a DDZJB-specific `ksud` is built. They are not a claim that those modules were
compiled against DDZJB. The app row says `test daemon` for that reason.
