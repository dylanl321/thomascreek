# Thomas Creek: rules for every agent

Read this first, whatever tool you are (Claude Code, Cursor, Codex or another). It is the constitution for the
Thomas Creek hub (`server/`) and the iPhone and iPad app (`ios/`). The TV apps live in
`dylanl321/thomascreek-tv` and follow the same rules from that repository's `AGENTS.md`.

## What we are building

One home app for the family, and one place to watch everything: IPTV live and films, Plex, kids' YouTube and
recordings, the same on the Android TVs, the Apple TVs and the iPhone. It should feel like Netflix and feel
magical, not just work: nothing blank, no dead taps, the right thing already on screen for the person using it.

| Part | Where | Job |
|---|---|---|
| Hub | `server/` | The one brain. Holds every secret, every list and every decision about what a person sees. |
| iPhone and iPad app | `ios/` | Remote, planner and window onto the same home the TVs show. |
| TV apps | `dylanl321/thomascreek-tv` | Android TV and Apple TV. They draw what the hub sends and play it. |

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
   same endpoint, not from its own logic. Send-to-TV, Now on TVs and guide editing go through the hub.
7. **Done means seen for real.** Hub changes are checked against the live hub from the home network after
   deploy; app changes in the simulator or on a device, with what was seen written in the pull request.
8. **Every failure says what happens next.** No silent errors: a failed call returns a short reason the apps
   can show, and the hub logs enough to find the cause.
9. **Kids first.** Child profiles never get an adult title from any endpoint. Filtering happens here.
10. **No secrets, addresses or hostnames in the repository.** Keys are added by Dylan on the admin page.

## Checklist for every pull request

- [ ] Decisions about what to show live on the hub, with tests.
- [ ] New fields are optional; existing clients keep working.
- [ ] The TV repository's `HubContract.kt` and the iPhone app are updated, or the gap is written down.
- [ ] Checked against the live hub after deploy.
- [ ] No secrets, addresses or hostnames.
