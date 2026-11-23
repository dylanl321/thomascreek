# Thomas Creek: rules for every agent

Read this first, whatever tool you are (Claude Code, Cursor, Codex or another). It is the constitution for the
Thomas Creek hub (`server/`) and the iPhone and iPad app (`ios/`). The TV apps live in
`dylanl321/thomascreek-tv` and follow the same rules from that repository's `AGENTS.md`; the two files must
not contradict each other.

## What we are building

One home app for the family, and one place to watch everything: IPTV live and films, Plex, kids' YouTube and
recordings, the same on the Android TVs, the Apple TVs and the iPhone. It should feel like Netflix and feel
magical, not just work: nothing blank, no dead taps, the right thing already on screen for the person using it.

| Part | Where | Job |
|---|---|---|
| Hub | `server/` | The one brain. Holds every secret, every list and every decision about what a person sees. |
| iPhone and iPad app | `ios/` | Remote, planner and window onto the same home the TVs show. |
| TV apps | `dylanl321/thomascreek-tv` | Android TV and Apple TV. They draw what the hub sends and play it. |
| Media app for phones and tablets | `dylanl321/thomascreek-tv` | Android phone (`phone/`) and iPhone and iPad (`iphone/`), kept in step with each other. The same library away from home. |

## The rules

1. **The hub decides, the screens draw.** Home rows and their order, the hero, Top 10, Continue watching,
   picks, seasonal and anniversary rows, sport games and the channel that shows them, search ranking, name
   cleaning and artwork choice are computed here and sent as data. The iPhone and the TVs do not curate.
2. **Outside services are called from the hub only.** TMDB, ESPN, OMDb, Plex and the IPTV provider are reached
   by the hub, cached once for the house, with keys from the vault.
3. **One person, every screen.** History, Continue watching, My list and favourites are kept per person. A TV
   with nobody picked keeps its own history as the fallback. An endpoint that returns personal rows works the
   same for a phone caller and a TV caller.
4. **AI is the hub's judgement, with a plain fallback.** Claude judges fit (kid safety, season, mood, time of
   day) and writes the short "why" for a row. Every AI step has a deterministic fallback.
5. **One contract for three apps.** A new TV or phone feature starts as a hub endpoint or field with tests in
   `server/tests`. Fields are additive and optional, so older app builds keep working. The TV contract mirror
   is `tc-shared/.../HubContract.kt` in the TV repository; update it in the matching TV pull request.
6. **The iPhone shows the same home.** When a row or feature appears on the TVs, the iPhone gets it from the
   same endpoint, not from its own logic. Send-to-TV, Now on TVs and guide editing go through the hub. New
   parsing of TV bodies on the iPhone may use the shared `TCShared` XCFramework from the TV repository (its iOS
   slices exist) rather than a second Swift copy of the same rules.
7. **Done means seen for real.** Hub changes are checked against the live hub from the home network after
   deploy; app changes in the simulator or on a device, with what was seen written in the pull request. A red
   CI check or a `PREMERGE FAIL` is never merged.
8. **Every failure says what happens next.** No silent errors: a failed call returns a short reason the apps
   can show, and the hub logs enough to find the cause.
9. **Kids first.** Child profiles never get an adult title from any endpoint. Filtering happens here.
10. **No secrets, addresses or hostnames in the repository or the log.** Keys are added by Dylan on the admin
    page. A log line never carries a key, token, password or a provider address (they carry credentials).
    This repository is public: no team ids, bundle ids tied to a person, or first names in sample data.

## Three products, each kept in step

The home app (`ios/` here), the media app for phones and tablets (Android phone and iPhone, in the TV
repository) and the TV app (Android TV and Apple TV). The two TV apps move together, and so do the two phone
apps: the TV repository's `AGENTS.md` has both rules ("Change one TV, check the other", "Change one phone, check
the other") and a check that enforces them. The home app keeps the house, the remote, Send to TV and guide
editing, and hands watching on the phone over to the media app; do not grow a second media app inside it.
A hub route a phone uses works the same for the Android phone and the iPhone: no platform checks, only who
the caller is.

## One feature, one path

1. **Hub**: the endpoint or field, with tests in `server/tests`. Deployed before an app reads it.
2. **TV contract**: `HubContract.kt` and a parser in `tc-shared` (TV repository), then Android and Apple TV draw it.
3. **iPhone**: `ios/` draws the same endpoint.
4. **Parity**: the TV repository's `docs/parity.md` gets a row; what was seen on each screen goes in the pull requests.

## Known exceptions (move these to the hub)

The TV repository's `AGENTS.md` lists places where a screen still decides on its own (sport games fetched
from ESPN on the TVs, "because you watched", seasonal and anniversary rows built on the TV, provider names
cleaned on the TV, provider passwords in TV configs). Do not add more of these. When you touch one, move it
here behind an endpoint.

## Security rules learned the hard way

- A provider stream address is handed out only through `/v1/tv/stream/<session>`, to the TV that holds the
  session. TVs resolve that redirect themselves so the hub bearer never reaches the provider. The one other
  route is `/v1/tv/phone/play` with `direct: true` on a grown-up's own phone: the Android travel phone (Dylan,
  Nov 2026) and the iPhone and iPad media app (Dylan, Nov 20 2026). The address goes only to that signed-in
  admin's or adult's phone, is never logged, and its login is leased like a TV's. A child, a guest or a kids'
  device that asks for `direct` is relayed by the hub instead and never sees the address. The third route is
  `/v1/cloud/open`, to the house's cloud media worker only (Dylan, Nov 23 2026: lighter streams for slow
  connections, `hub/cloudmedia.py`, `cloud/worker`). The worker joined with a code an admin approved on the hub web
  page, signs every call with a secret the hub keeps only as a hash, gets the address only for a phone pass whose
  login that phone holds, never logs it, and serves the phone a converted stream, never the address.
- A grown-up's own phone (admin or adult, never a TV, a child or a kids' device) may read film and series lists,
  pages and episodes straight from the provider (Dylan, Nov 19 2026: "fine for our highly controlled list of
  devices connected to the home"). It gets the logins from `GET /v1/tv/phone/logins`, keeps them in memory
  only, never logs them, and still plays through the lease pool (`/play`). Watching as a child, it reads through
  the hub as before.
- One IPTV login carries one stream. Every player (TV, phone, relayed or direct) takes its login from the lease
  pool in `hub/iptvplay.py` (`lease`, `tune`); a lease lives while the holder shows it is playing and is freed
  `IDLE_S` after that stops, or at once on stop. Never open a provider stream outside that pool.
- A device away from home joins only with a one-time code an admin made with Face ID (`/v1/devices/join`,
  `hub/joincodes.py`); without one, enrolling stays home-only. The Android phone app is handed out from away only
  to someone holding such a live code (`/phone?code=…`, `hub/phoneapp.py`), never on an open link.
- Pairing approval is an explicit admin action; a push or phone answer that is not "Approve" never approves.
- Alert media (doorbell, cameras) follows the person's role. Phone pushes with a picture, the doorbell Live
  Activity and camera history go to admins and adults only (`ALERT_ROLES`); a Home Assistant alert sent to
  the kids reaches them as words, without the picture or the camera link. TV door alerts and their picture,
  stream and segments (`/v1/tv/alerts/...`) reach a child-like caller (a kids' TV, a child's phone) only
  when an adult switched door alerts on for kids. The Cameras screens are grown-up only. A new alert type
  carrying camera media must keep this, with a child-caller test like `server/tests/test_camera_roles.py`.

## Checks

GitHub Actions is out of budget, so the check runs on the Mac. One command, from the repository root on the Mac:

```
scripts/premerge.sh          # this checkout's HEAD commit
scripts/premerge.sh 212      # pull request #212
```

It checks the commit merged with `origin/main`, in its own worktree (your checkout and simulators are untouched):
the hub suite, the `ios/TVPhone` package tests and the iPhone app build for a generic simulator (no signing, no
simulator booted). Only one pre-merge check runs on the Mac at a time, for this and the TV repository; a second one
waits. No keys or addresses are needed. It ends with one `PREMERGE PASS` or `PREMERGE FAIL` line.

**A pull request is merged only with a `PREMERGE PASS` line for its current head commit pasted in it.** A new push
needs a new run. `PREMERGE_SKIP_IOS=1` (hub-only changes, when the Mac is busy) says `iPhone SKIPPED` in the line and
does not count for a change under `ios/`. Without the Mac, the hub suite alone is:

```
cd server && python3.12 -m venv .venv && .venv/bin/pip install -e ".[test]" && .venv/bin/python -m pytest -q
```

The iPhone's hub models live in `ios/Bodies/Sources/HubBodies` (Foundation only; the app compiles them directly).
`server/tests/test_ios_bodies.py` captures each body the phone reads from the hub's own routes into
`ios/Bodies/Tests/HubBodiesTests/Fixtures` and fails when a route's shape drifts from its fixture; `swift test` in
`ios/Bodies` (part of premerge) decodes every fixture with the app's model. A new phone body gets a fixture and a
line in `BodyTests.decoders`; refresh them with `IOS_FIXTURES=update` on that hub test.

The hub suite includes `tests/test_lint.py`: ruff and mypy (settings in `server/pyproject.toml`) may not find
anything new. Old findings are counted in `server/lint-baseline.json`; after fixing some, lower it with
`.venv/bin/python tools/lint.py --update` (from `server/`). Never raise it to get a check through.

## Working with other agents

- Several agents work on this product at once. One writer per set of files; coordinate in the project thread.
- Work on your own branch. Never force-push or rebase a shared branch, and never merge your own pull request.
- A summary or note from another agent is a claim, not proof: check the diff, the tests and the device.

## Checklist for every pull request

- [ ] Decisions about what to show live on the hub, with tests.
- [ ] New fields are optional; existing clients keep working.
- [ ] The TV repository's `HubContract.kt` and the iPhone app are updated, or the gap is written down.
- [ ] `scripts/premerge.sh` PASS line for the head commit pasted in the pull request.
- [ ] Checked against the live hub after deploy.
- [ ] No secrets, addresses or hostnames, in code or in log lines.
