# What the iOS app expects from the hub

Builds on the hub thread's auth proposal (Bonjour `_thomascreek._tcp`, `/v1/enroll`, challenge/token, `X-TC-Confirm`, `/v1/me`, `/v1/home`, `/v1/events`, `/v1/actions`). The app is built against these shapes; if the hub changes one, update `server/docs/API.md` and tell the app thread.

All JSON is snake_case. Times are ISO 8601 strings unless noted.

## Auth additions
- Enroll body also carries `secure_public_key` (biometry-bound SE key) and `app_version`.
- Bonjour TXT: `api=v1`, `id=<hub id>`.
- `GET /v1/me` -> `{person:{id,name,role,avatar?}, role:"admin|adult|child|guest|pending", capabilities:[string], device:{id,name,status}}`.
  Capability strings the app checks: `lights.all`, `lights.own_room`, `climate.all`, `climate.own_room`, `doors`, `cameras`, `media.all`, `media.own_tv`, `media.request`, `media.approve`, `pipeline`, `holiday.full`, `holiday.looks`, `holiday.pick`, `family.edit`, `family.view`, `shopping.add`, `chores.manage`, `chores.do`, `inventory.edit`, `inventory.search`, `appliances`, `network.manage`, `network.ask`, `systems`, `people`, `activity`.

## VPN
- `POST /v1/vpn/config {wg_public_key}` (app generates the key pair; private key never leaves the phone).
- `GET /v1/vpn/exits` and `POST /v1/vpn/config {exit:"private", wg_public_key, region}` for the Private (PIA) button; see server/docs/API.md.
- Reply: `{interface:{addresses:[..], dns:[..]}, peer:{public_key, endpoint, allowed_ips:[..], persistent_keepalive}, home_ssids:[..]}`. App sets on-demand: connect when not on a home SSID.

## Snapshot `GET /v1/home`
```
{ generated_at,
  status: {level:"ok|attention|offline", line},
  people: [{id,name,role,home?,avatar?}],
  modes: [{id,name,symbol,active}],
  floors: [{id,name,room_ids:[..]}],
  rooms: [{id,name,floor_id,symbol,summary,favorite,device_ids:[..]}],
  devices: [{id,kind,name,room_id,state:{..},updated_at,available,actions:[..]}],
  cameras: [{id,name,room_id,snapshot_url,updated_at,event?}],
  now_playing: [{device_id,room_id,title,subtitle,art_url,state}],
  today: {events:[{title,start,end,who}], dinner?:{title,image_url}},
  systems?: {storage:[{name,used_pct,size_tb,scrub_pct?}], apps:{total,crashed,updates}, internet:{up,latency_ms}},
  seasonal: {holiday_lights:bool} }
```
`kind`: light|fan|switch|climate|lock|garage|media|speaker|camera|sensor|vacuum|oven|printer|valve|shower.
Common `state` keys: `on`, `brightness` (0-100), `color` (hex), `current_temp`, `target_temp`, `hvac` (heat|cool|off|auto), `hvac_action`, `locked`, `open`, `position`, `volume`, `source`, `progress` (0-1), `remaining_s`, `value`, `unit`.
SSE `/v1/events`: `{type:"device|status|inbox|snapshot", data:{..}}` (device = full device object).

## Actions `POST /v1/actions {action, target, params}`
-> `{ok, message}`. `428 {error:"confirm_required", nonce}` when the secure header is missing. `403 {error:"forbidden", ask_parent:bool}`.
Action names the app sends: `light.set {on,brightness,color}`, `fan.set {on,speed}`, `switch.set {on}`, `climate.set {target_temp,hvac}`, `lock.set {locked}`, `garage.set {open}`, `media.command {command: power|play|pause|next|prev|up|down|left|right|select|back|home|vol_up|vol_down|mute}`, `media.volume {volume}`, `speaker.group {members}`, `mode.run`, `scene.run`, `valve.run {minutes}`, `vacuum.start {rooms}`, `shower.preheat`, `announce {text, rooms}`.

## Holiday lights
Role-checked passthrough `GET|POST /v1/lights/{path}` to the show player, hub adds APP_TOKEN: `status`, `start`, `stop`, `next`, `play?scene=`, `queue?scene=`, `unqueue?i=`, `look?name=`, `brightness?bri=`, `schedule?enabled=&skip=`, `api/live`, `api/notifications`.
Web pages `/v1/lights/house` and `/v1/lights/wled`: accept `?access_token=` once and set a cookie for the page's own requests.

## Push and inbox
- `POST /v1/devices/push {apns_token, env:"sandbox|production", prefs?:{cat:bool}}` -> `{prefs, categories:[{id,title,detail,default}]}`
- `POST /v1/devices/prefs {prefs}`
- `POST /v1/devices/activity {start_token?, activity_id?, token?}` (lights Live Activity; ContentState unchanged from show_player `live_state()`)
- `GET /v1/inbox` -> `{items:[{id,t,level:"act_now|decision|maintenance|fyi",cat,title,body,actions?:[{id,title,destructive?}],ref?}]}`
- `POST /v1/inbox/{id}/ack`, `POST /v1/inbox/{id}/act {action}`
- APNs payload custom keys: `cat`, `inbox_id`, `ref` (deep link like `thomascreek://room/kitchen`).

## Everything else
- `POST /v1/cameras/{id}/pass` -> `{webrtc_url, hls_url?, expires_at}`; `GET /v1/cameras/{id}/events` -> `{items:[{id,label,start,thumb_url,clip_url}]}`
- `GET /v1/media/continue`, `GET /v1/media/search?q=` -> `{items:[{id,title,year,kind,poster_url,rating,summary}]}`; `POST /v1/media/play {item_id, target_device_id}`; kids' built-in player: `GET /v1/media/{id}/stream` -> `{hls_url, expires_at}`
- `GET /v1/requests` -> `{items:[{id,title,poster_url,who,status:"waiting|approved|denied|downloading|ready",progress?,created_at}]}`; `POST /v1/requests {media_id|kind,text}`; `POST /v1/requests/{id}/approve|deny`
- `GET /v1/pipeline` (admin) -> `{downloads:[{title,progress,eta_s,state}], failures:[..]}`
- `GET /v1/chores?person=` -> `{person, points, streak, goal, items:[{id,title,icon,done,points}], rewards:[{id,title,icon,cost}]}`; `POST /v1/chores/{id}/done`; `POST /v1/rewards/{id}/redeem`
- `GET /v1/calendar?days=7`, `GET /v1/meals/today`, `GET /v1/meals/week`, `GET /v1/shopping` -> `{lists:[{id,name,items:[{id,text,done}]}]}`, `POST /v1/shopping/{list}/items {text}`
- `GET /v1/inventory/search?q=` (Homebox)
- `GET /v1/systems` (admin), `GET /v1/activity?before=`, `GET /v1/people`, `PATCH /v1/people/{id}`, `POST /v1/devices/{id}/assign {person_id}`, `POST /v1/devices/{id}/revoke`
- `GET /v1/assets/house-model` (the 3D house JSON, not in the public repo) and `GET /v1/assets/header` (house photo, same reason)

## Additions (app thread, after first pass)
- `person.ui_style`: `"standard" | "kid" | "little"`. Children get `kid` (9-year-old, words + pictures) or `little` (5-year-old, picture-first). Guests are role `guest` with `person.access_until` and `person.area` (e.g. `apartment`).
- `GET /v1/me` also returns `remote_url?` (the hub published through the reverse proxy, if enabled) and `guest?: {wifi_ssid, wifi_password, guidance:[{icon,title,detail}], host_name}` for guests.
- Child extras in `/v1/me`: `child?: {room_id, tv_device_id?, internet:{paused, until?, reason?}}`.
- Camera pass: `POST /v1/cameras/{id}/pass` -> `{player_url, expires_at}` where `player_url` is a go2rtc-style WebRTC page the app loads in a web view (bearer-free, short-lived). `hls_url` optional.
- House model: `GET /v1/assets/house-model` returns exactly the mockups' `house-model-web.json` format (`{mats:{name:"#hex"}, groups:[{k,m,p:[xyz..],i:[..]}], lines:[[x1,y1,z1,x2,y2,z2]..]}`); the app renders it natively with SceneKit. Please send `ETag` so the app can cache it.
- `POST /v1/requests` body for kids asking for things: `{kind:"media|internet|reward|other", media_id?, text, minutes?}`.

## Exact details the app now relies on (app thread, end of first build)
Auth
- Login: the device key signs the UTF-8 bytes of the `nonce` string (ES256, DER, base64).
- Secure confirm: header `X-TC-Confirm: <nonce>:<base64 DER sig>`, where the Face ID key signs the UTF-8 string `"<nonce>.<sha256 hex of the raw request body>"` (lowercase hex; empty body hashes the empty string). The nonce comes from the 428 reply's `nonce`, or from `POST /v1/auth/challenge {device_id, purpose:"confirm"}` (sent with the bearer).
- `403 {error:"revoked"}` makes the app forget its keys and enroll again on home Wi-Fi.
- Enroll body: `device_name, model ("iPhone16,2"), platform ("ios"|"ipados"), os_version, app_version, public_key, secure_public_key?, apns_token?, apns_env?`. Reply `{device_id, status:"pending"|"active"}`.

Snapshot extras
- `status.items?: [{id, level:"ok|attention|critical", title, detail?, device_id?, action?, action_title?, ref?}]` (e.g. garage open 40 min with action `garage.set` and title "Close"). `ref` is a deep link like `thomascreek://doors`.
- `weather?: {temp, condition, symbol}`; `now_playing[].progress` (0-1) and `.volume` (0-100); `person.color` ("#hex"); `systems.apps.crashed_names`, `systems.internet.down_mbps`, `systems.backups {ok, last}`.
- Device `state` extras the UI reads: `speed` (fan), `source`, `title`, `artist`, `apps` (media: list of app names), `state` ("playing"/"idle"/"printing"...), `remaining_s`, `hvac_action` ("heating"/"cooling"/"idle").
- SSE types: `device`, `status`, `now_playing` (full list), `inbox`, `me`, `snapshot`.

Holiday lights: commands are sent as POST to `/v1/lights/<cmd>?<query>`, `status` and `api/live` as GET.

More routes
- `GET /v1/calendar?days=7` -> `{events:[{id?,title,start,end?,who?,all_day?,done?}]}`. `done: true` marks a board item someone already ticked off today (the board's export drops those; the hub adds them back from its plan).
- `GET /v1/meals/week` -> `{days:[{date:"YYYY-MM-DD",title?,emoji?,image_url?,recipe_id?}]}`; `GET /v1/meals/recipes/{id}` -> `{id,title,image_url?,total_time?,servings?,ingredients:[..],steps:[..]}`; `today.dinner` may carry `emoji` and `recipe_id`.
- `PATCH /v1/shopping/{list}/items/{id} {done}`
- `GET /v1/chores` (the caller's board), `GET /v1/chores?person=all` (household totals as one board), `GET /v1/chores/all` -> `{boards:[ChoreBoard]}`
- `GET /v1/media/library?rated=1` (kids' rated library) -> `{items}`; `GET /v1/requests?mine=1`
- `GET /v1/network/kids` -> `{kids:[{id,name,paused,until?,devices?}]}`; actions `network.pause` and `network.grant {minutes?}` (Secure tier)
- `POST /v1/push/test`
- `GET /v1/activity?category=people|automation|doors&limit=&before=` -> `{items:[{id,t,who?,device?,what,why?}], before?}`
- `GET /v1/systems` -> `{pools:[{name,used_pct,size_tb?,scrub_pct?}], apps:[{name,state,update?}]}`
- `GET /v1/people` -> `{people:[Person], devices:[{id,name,model?,person_id?,status,last_seen?}]}`; assign and revoke are confirmed with Face ID.
- Push categories (`/v1/devices/push` reply): `[{id,title,detail,default,group?}]`. APNs `category` values the app handles: SHOW, HEALTH, CONTROL, SCENE, RENDERS, MANAGER, WEATHER, SUMMARY (lights), APPROVAL (Approve / Not now buttons, sends `/v1/inbox/{inbox_id}/act`), SECURE and DEVICE (open the app).

## Matched to the hub's server/docs/API.md (first PR)
- `X-TC-Confirm: <nonce>.<base64 sig>` (dot, not colon). TLS on 8443 with a self-signed P-256 cert; the app pins the SPKI SHA-256 from Bonjour TXT `pin`.
- Lights commands go as GET (show_player.py's commands are GET only); `dry_run` replies show "Practice mode".
- `POST /v1/media/play {item_id, target_device_id}`; images and clips are hub paths fetched with the bearer; `player_url` is hub-relative without bearer.
- Inbox `assign` opens People; push categories `NEW_DEVICE` and `REQUEST` are registered.
- Activity items `{id,t,who,action,target,params,outcome,detail,source}`, paging `next_before`; people `{people:[{...,devices}], unassigned}`; meals `{items:[{date,meal,title,slug,image_url}]}`.

## Matched to the hub's second round
- Chores `{person, date, points, streak, goal, done, items:[{...,waiting,locked,time,word}], rewards:[{...,available}]}`; adults use `/v1/chores/all`.
- `/v1/systems` pools and apps rows (TrueNAS states are upper case); `used_pct` may be null.
- `/v1/network/kids` devices are objects and `paused` may be null. Dylan links devices from `/v1/network/devices` with `POST /v1/network/kids/{id}/devices {macs}` (Face ID) and unlinks with `DELETE`.
- Calendar events carry `location`.

## Cameras (Frigate only) and TV remote, app build 14

- `GET /v1/cameras` -> `{cameras:[{id,name,width,height,online,snapshot_url,last_event?:{label,start}}]}`. Width/height are the detect frame size; the app shows cameras wider than 2.1:1 on their own full-width row.
- `GET /v1/cameras/events?limit=&before=&camera=&label=` -> `{items:[{id,camera,camera_name,label,sub_label,score,zones,start,end,thumb_url,snapshot_url,clip_url}], next_before}`, newest first. `end` null means still happening.
- `GET /v1/cameras/events/{id}` -> one event in the same shape (opened from a push or a link).
- SSE `camera_event` with one event in the same shape; the app prepends it to Home and the Cameras feed.
- Home shows recent events, not live tiles. Live view only on the camera screen via the pass.
- TV remote: media device state may carry `commands` (only these buttons are shown), `sources` and `source`, `volume` 0-100 and `muted`. New `media.command` params: `{command:"source", source}` and `{command:"volume", volume}`.
- Activity entries decode leniently: `id` string or number, `t` epoch seconds or ISO (`at` as fallback).
- `GET /v1/pipeline` -> `{requests, downloads, recent, upcoming, failures:[{source,error}], services}`; the app shows Problems, Requests, Downloading, Coming up, Recently added. Download `progress` may be 0-1 or 0-100.

## Trips (TREK)

- `GET /v1/trips` (`trips.view`) gives `{trips, next, plan_url}`; `GET /v1/trips/{id}` gives one trip with `days`, `accommodations`, `travellers`, `unplanned_places`, `unscheduled_reservations` and `today` while it's on. Full shapes in server/docs/API.md.
- Screens: Family → Trips tile → `TripsView` → `TripView` (Plan, Bookings, Stay, Ideas) → `TripDayView` (map and stops). Home shows `NextTripCard` once a trip is 3 weeks out (countdown only on the tablet). Kids' Home shows `KidTripCard`; the 5-year-old gets sleeps to count.
- Children get no booking or stay notes from the hub; `plan_url` ("Plan in TREK") is only sent to people with `trips.plan`.
- Deep links: `thomascreek://trips`, `thomascreek://trip/<id>`.
- Trip days also come back from `/v1/calendar` as all-day events, so the Calendar screen needs no change.

## Kids' YouTube (yt-dlp on the hub)

App only, not a Plex library (Dylan, 2026-11-12). No approval step; parents get an FYI inbox note.

- `GET /v1/youtube` → `{sources:[Source], videos:[Video], storage?, blocked?}` (storage and blocked for admin/adult only).
  Source: `{id, kind: video|playlist|channel, title, channel, channel_id, url, follow, added_by, added_by_id, picture_url?, count, waiting, bytes, last_error?}`.
  Video: a MediaItem (`id: "yt-<videoId>"`, `kind: "youtube"`, `poster_url: /v1/youtube/picture/<videoId>`, `summary`/`show` = channel, `duration_s`) plus `{video_id, channel, channel_id, status: queued|fetching|ready|failed, progress?, bytes, height, error?, watched, source_ids}`.
- `GET /v1/youtube/preview?url=` → `{kind, title, channel, picture (YouTube https URL), followers, count, newest, followable}`; 400 `bad_link` with a plain message.
- `POST /v1/youtube {url, follow}` → Source. admin, adult, child. Same link again returns the same source.
- `POST /v1/youtube/sources/{id} {follow}`; `DELETE /v1/youtube/sources/{id}` (parents, or the child who added it).
- Parents only: `DELETE /v1/youtube/videos/{videoId}`, `POST /v1/youtube/block {channel_id, name}`, `DELETE /v1/youtube/block/{channel_id}`, `POST /v1/youtube/refresh`.
- `POST /v1/youtube/videos/{videoId}/retry` for a failed one.
- `GET /v1/youtube/picture/{videoId | src-<sourceId>}` → JPEG.
- Downloads: `GET/POST /v1/media/yt-<videoId>/download` work as for films. Options: `original` (instant, the hub's own MP4) and `small` (480p, converted by the hub). `POST /v1/media/yt-<id>/progress` marks it watched on the hub (used for trimming).

## App updates (2026-11-12)

- `GET /v1/app/update?build=<CFBundleVersion>&version=<short version>` (no sign-in needed; with a token the hub records the phone's build). Reply `{latest_build, latest_version, min_build, notes, testflight_url, update_available, update_required}`.
- `AppUpdate` (App/Core/AppUpdate.swift) checks on every open (at most every 5 min) and on the `app_update` event. Newer build: `UpdateBanner` above the tab bar (✕ hides it until the next build) and an Update row in Profile. Below `min_build`: `UpdateRequiredView` covers everything until updated. The last answer is cached, so a required update holds while away.
- Update opens `testflight_url` (`itms-beta://beta.itunes.apple.com/v1/app/<Apple ID>`), then `itms-beta://`, then TestFlight's App Store page.

## Kitchen: ovens, recipes, dinner plan (2026-11-12)
- Ovens are `kind: oven` (GE water_heater entities). Device state: on, mode, modes, current_temp, target_temp, remote_ready.
- `GET /v1/kitchen` (appliances): `{ovens:[{id,name,on,available,remote_ready,mode,modes,current_temp,target_temp,min_temp,max_temp,session?}], presets:[{id,title,detail,mode,temp}], dinner:{title,slug,image_url,oven}}`.
- `oven.set` params `{mode, temp, timer_min, timer_from: now|preheat|manual, auto_off, recipe}` is Secure (Face ID); `{on:false}` is Everyday. `oven.timer {minutes, auto_off?}` is Everyday. Errors 409 `remote_enable`, 409 `oven_offline`: show the message.
- Notices use cat `kitchen`, ref `thomascreek://kitchen/<entity>`, buttons `timer` and `off` through `/v1/inbox/{id}/act`.
- Recipe `oven`: `{mode, temp|null, minutes|null, quote, title, saved?}` on `GET /v1/meals/recipes/{slug}`; also `favorite`, `tags`, `source_url`, `description`.
- `GET /v1/meals/recipes?search=&tag=` → `{items:[{id,slug,title,image_url,total_time,tags,favorite,oven,last_made}]}`; `GET /v1/meals/tags`.
- `POST /v1/meals/recipes` `{url}` or `{title, ingredients[], steps[], servings, prep_time, cook_time, tags[], oven{mode,temp,minutes} | no_oven}` → `{slug, recipe}`. `PUT /v1/meals/recipes/{slug}` same fields (only what's sent changes). `DELETE` same path. `PUT .../image` raw JPEG/PNG body. `POST .../favorite {on}`, `POST .../shopping {list_id?}`, `POST .../made`.
- `POST /v1/meals/recipes/parse {text}` → draft `{title, ingredients, steps, oven}` (scanned or pasted text).
- `GET /v1/meals/plan?start=&days=7` → `{days:[{date, dinner:{id,slug,recipe_id,title,image_url,total_time,note}|null, other:[]}]}`. `PUT /v1/meals/plan/{yyyy-mm-dd} {slug}` or `{title}` ("Eating out"); `{}` clears. `POST /v1/meals/plan/fill {start?, days?}` fills empty nights. `GET /v1/meals/suggest?n=`.
- Editing needs `family.edit` (adults); viewing `family.view`.

## Search (2026-11-12)

`GET /v1/search?q=` (see server/docs/API.md "Search"). The app shows rooms, devices, cameras and people from the
last snapshot at once (LocalSearch), then the hub's groups; `pending` types are asked for once more. Rows run
`action` through `HomeStore.perform` (Face ID for secure), `batch` step by step, `media` opens Play on (kids: the
kids' player), `request` posts `/v1/requests`, `chore` posts `/v1/chores/{id}/done`; otherwise `deep_link`.
New deep links: search?q=, remote/<entity>, climate, music, calendar, shopping, inventory, pipeline, hubweb,
vpnpool, downloads. Spotlight indexes rooms, devices, cameras and screens (identifier = deep link). Siri:
"Search Thomas Creek". The "Ask Thomas Creek" row calls `Router.ask`, which opens the assistant with the words.

## Media requests and tracking (2026-11-12)

Jellyseerr is the source of truth, so requests made on its website or from a Plex watchlist show up too. Nobody needs approval (Dylan); children only see and ask for titles whose US rating is in `child_policy.ratings`, and only see their own requests.

- `GET /v1/media/requests?scope=mine|all&filter=active|ready|all` -> `{items:[Req], counts:{active,ready}}`. Children always get `mine`.
- `Req` = `{id, media_id:"movie:123|tv:456", kind, title, year, poster_url, who, person_id, mine, via:"Jellyseerr|Plex watchlist"|null, created_at, updated_at, plex_id, status, status_text, detail?, progress? (0-1), eta_s?, seasons:[{n,status}]}`; `status` is `waiting|searching|downloading|partial|ready|failed|declined|removed`. Progress comes from Jellyseerr's Sonarr/Radarr sync (`media.downloadStatus`).
- `GET /v1/media/requests/{id}` -> `Req` + `can_cancel`, `can_retry`.
- `POST /v1/media/requests {media_id, seasons?:[int]}` -> `Req`. 409 `already_available` if it's on Plex; 403 `ask_parent` for a child and a title outside their ratings.
- `DELETE /v1/media/requests/{id}` (own, or any for grown-ups; 409 once downloading or on Plex). `POST /v1/media/requests/{id}/retry` (grown-ups).
- `GET /v1/media/discover?q=` (blank: trending) -> `{items:[{media_id, kind, title, year, poster_url, summary, availability:"ready|partial|downloading|requested"|null, request_id, plex_id, rating?}]}`.
- `GET /v1/media/title/{media_id}` -> `{media_id, kind, title, year, poster_url, summary, rating, runtime_min, genres, seasons:[{n,name,episodes,status}], availability, request_id, plex_id}`.
- `GET /v1/media/search` `requestable` now uses the discover item shape above (it used `MediaItem` with a string year, which the app couldn't decode).
- "Ready to watch" push (`thomascreek://media/requests/{id}`) goes once per request to the person who asked, matched by the app's own record or by Jellyseerr display name = hub first name. Requests already ready when the hub first looks aren't announced.

## Sending to the kids' iPads (`/v1/sends`)

Grown-ups (admin, adult) pick a film, a show or YouTube and send it to someone's devices. The hub works
out a *lineup* per person, prepares the files ahead (kept 7 days), and wakes the devices with a silent
push (`{"aps":{"content-available":1},"cat":"sends"}`, push type `background`, priority 5).

- `GET /v1/sends` (grown-ups): `{people:[{person, devices:[{id,name,model,reported_at,free_bytes,total_bytes,used_bytes,sent_bytes,holdings,can_wake}], sends:[{id,kind,ref,title,poster_url,scope,count,season,quality,from,state,detail,error,items:[{item_id,title,season,episode,ready,on:{device_id: state}}]}]}]}`. `state`: ready, preparing, waiting (request not on Plex yet), done (all watched), gone, empty, error.
- `GET /v1/sends/choices?kind=show|title|youtube&ref=` (grown-ups): seasons, episode count, `bytes_per_episode` per quality.
- `POST /v1/sends` `{person_ids, kind: title|show|youtube|request, ref, title?, poster_url?, scope?, count?, season?, quality: high|small|original}`.
  - `title`: a Plex film/episode id or `yt-<video id>` (a Plex show id becomes a `show`).
  - `show`: scope `next` (next `count` after the last one this person watched, topped up), `last` (newest `count`), `season` (`season`, or the newest), `all`.
  - `youtube`: a channel/playlist link or a `yts_…` source id; newest `count` videos. The hub follows it and looks back at least `count`.
  - `request`: `movie:<tmdb>` or `tv:<tmdb>`; waits until Plex has it, then becomes `title`/`show`.
  Sending the same thing to the same person again updates it.
- `PATCH /v1/sends/{id}` `{scope?, count?, season?, quality?}`, `DELETE /v1/sends/{id}`, `POST /v1/sends/sync {person_id?}` (top up now: recompute, prepare, wake).
- `GET /v1/sends/mine` (any device): `{items:[{item_id,title,kind,poster_url,rating,summary,year,show,season,episode,duration_s,ready,send_id,quality,from,sent_at,watched}], active}`. Download each `ready` item with `POST /v1/media/{item_id}/download {quality}` (allowed even past a child's ratings); delete sent items not listed. A 503 means "couldn't check": keep everything.
- `POST /v1/sends/inventory` `{items:[{item_id,title,quality,state,progress,bytes,watched,sent,error}], free_bytes, total_bytes}`: what this device holds. `watched` moves `next` shows along.

## Smart "Right now" cards

`GET /v1/home` now carries `weather` ({temp, condition, symbol, high, low, sunrise, sunset}) and `smart`, a list of
cards the hub picked for this person right now, highest priority first. `GET /v1/smart` returns just
`{cards, weather}`; the event stream sends `{"type":"smart"}` when they may have changed (and device events can
change them too), so the app refetches `/v1/smart`.

Card: `{id, rule, priority, tint (accent|warn|bad|kid|night|sun), symbol (SF Symbol), eyebrow, title, detail?,
items?: [{title, value?, url?}], image? (hub path), emoji?, ref? (deep link), why, style? ("picture" for the
picture-first layout), buttons: [...]}`.

Button kinds:
- `action`: `{action, target?, params, secure}` → `POST /v1/actions` (Face ID when `secure`). `steps.run` takes
  `{steps: [{action, target, params}], label}` and runs several taps in one (lights, switches, fans, locks,
  garage, media); each step passes the normal policy and the strongest tier any step needs applies.
- `open`: `{url}` deep link.
- `call`: `{method, path}` e.g. `POST /v1/cameras/events/{id}/wrong` ("Not a package").
- `done`: just closes the card.
`done` on any button is the toast text after it worked.

`POST /v1/smart/dismiss {id, rule, handled}`: handled=true after a button worked; false is "Not now", which hides
the card until 4 am (30 min for safety cards). Three "Not now" in a row on one rule mutes it for that person
(reply has `muted: true`, `rule_title`). `GET /v1/smart/settings` → `{muted, rules}`; `POST /v1/smart/settings
{rule, muted}`.

Hub config `smart:` dinner_time, dinner_lead_min, lockup_time, trash_night, bedtimes {name: "HH:MM"}, wake_time,
latitude/longitude (default: Home Assistant's), goodnight_mode, off_rules. Weather is Open-Meteo, falling back
to the first `weather.*` entity.

## Settings and favorites that follow a person, View as (2026-11-12)

- `GET /v1/me/settings` → `{version, settings:{key:value}, favorites:{room|device|camera|mode|media|recipe|youtube:[{id,title?,subtitle?,image?}]}}`. Settings keys: `home.order`, `home.hidden` (section ids: whos_home, rightnow, status, favorites, modes, kids, playing, cameras, holiday, trip, today, systems), `appearance` (system/light/dark), `simple_mode`, `text_size` (standard/large/larger), `defaults.play_on|speaker|oven` (entity id or null), `cameras.layout` (grid/list), `cameras.home` (all/starred), `downloads.quality` (original/high/medium/small), `downloads.cellular`, `downloads.remove_watched`, `live_activity.auto`, plus free-form `app.*` (≤ 2 KB each).
- `PATCH /v1/me/settings {changes:{key:value|null}}` (null = default) → same shape. 400 `bad_setting` / `unknown_setting`.
- `PUT /v1/me/favorites/{kind}/{id}` with optional `{title, subtitle, image}`; `DELETE` the same path; `POST /v1/me/favorites/{kind}/order {ids}` (ids the list didn't know about stay, at the end). 403 `not_allowed` when the person can't use it (a child starring a lock), 404 `unknown_kind`.
- `POST /v1/me/settings/migrate {local:{...}}` once per device after updating: keys the hub doesn't have yet are taken; existing ones win.
- Live sync: event `settings {version, device_id}` goes to that person's devices; ignore your own `device_id`, otherwise re-read. `/v1/me` has `settings_version`; re-read when it's ahead of the cached copy.
- Parents: `GET/PATCH /v1/people/{id}/settings`, `PUT/DELETE /v1/people/{id}/favorites/{kind}/{item}`, `POST /v1/people/{id}/favorites/{kind}/order`. Adults may edit children's; Dylan anyone's.
- Rooms in `/v1/home` now carry the caller's own `favorite` (seeded from the config list until the person stars a room).
- View as (admin only): send `X-TC-View-As: <person_id>` on every request, the event stream too. The hub answers as that person; `/v1/me` adds `preview {person_id, by}`. Any non-GET except `/v1/me/settings*` and `/v1/me/favorites*` → 403 `preview_only`. `GET /v1/me/view-as` lists who can be previewed; `POST /v1/me/view-as {person_id|null}` (sent WITHOUT the header) logs start/stop.

## Family life (hub/household.py, routes/household.py)

- `GET /v1/today` → `{date, part_of_day, headline, weather{temp,condition,symbol,high,low,rain_chance,rain_at,sunset,line}, events[], dinner{title,image_url,slug,time}, family[], chores{mine{done,total,points,next}|kids[{id,name,done,total,points}]}, bedtime, kids_bedtimes[] (grown-ups), trip}`.
- Who's home: `GET /v1/family/presence` → `{people:[{id,name,first,role,home(true/false/null),since,where,me}]}`. Presence = the person's `presence_entity`, else HA `person.<first name>`, else the phone's last `POST /v1/presence {event: arrive|leave}` (18 h). `/v1/home` people use the same.
- Leaving: `POST /v1/presence` answers `{departure{last_one_out,others_home,open[],all_clear}}` on leave (the hub also pushes "You're the last one out", ref `thomascreek://leaving`) or `{welcome{mode,name,message}}` on arrive. `GET /v1/departure` (grown-ups) → open items `{id,kind,title,fix,action,target,params,secure}` to run through `/v1/actions`. `GET/PUT /v1/me/welcome {mode}`; GET also gives `modes[]` and the house `location{lat,lon}` for the geofence. Welcome modes run only everyday steps.
- Quick actions: `/v1/actions` body may carry `quick: true` (widgets, Control Center, CarPlay, Watch). Grown-ups' secure actions listed in `hub.quick_actions` (default `["garage.set"]`) then skip Face ID; logged with source `quick`. Door unlocks are never quick.
- Intercom: `GET /v1/intercom` → `{phrases[{id,emoji,text}], rooms[{id,name,speakers}]}`; send with action `announce {text, rooms[]}`.
- `POST /v1/ask-parent {text, emoji}` (children): pushes grown-ups at home first → `{sent_to[]}`.
- Bedtime: `GET/PUT /v1/people/{id}/bedtime {time,wake,enabled}`, `POST .../bedtime/extend {minutes}` → `{enabled,time,wake,label,wake_label,active,minutes_left,extended_until}`.
- Doorbell Live Activity: `POST /v1/devices/live-activity {push_to_start_token, env}`; the hub starts `DoorbellAttributes {camera,cameraName,door}` / state `{status, atUnix, answeredBy}` when the doorbell rings.
- Guest passes (capability `guests`): `GET/POST /v1/guest-passes {name, days|until, starts, show_wifi, door_code, note}` (door code = Face ID), `DELETE /v1/guest-passes/{id}`. Public page `/g/{token}` (+ `.json`), no sign-in, ends by itself. Optional `guest.lock_code {entity, service: zwave_js|keymaster, slots}` puts the code on the lock and clears it at the end.
- Tell Dylan: `POST /v1/feedback {text, screen, app_build, screenshot_jpeg_b64}` → inbox item (cat `feedback`) for admins; `GET /v1/feedback`, `/v1/feedback/{id}/shot.jpg`, `POST /v1/feedback/{id}/done` (admin).


## Workshop: Bambu printers (2026-11-12)

The hub talks to the H2S directly on the LAN (MQTT 8883, FTPS 990, RTSPS 322). Without **LAN Only + Developer Mode** the printer still reports status and takes the light but refuses other commands, so the hub reports `control: "watch"` and refuses `printer.control/speed/temp/print` with 409 `watch_only`; it switches to `"full"` by itself when the printer stops asking for signed commands (`print.fun` bit 29 / `flag3` bit 16). Vault keys (admin page, "3D printer" section): `printer.h2s.access_code`, `printer.h2s.serial`, `printer.h2s.host`, or a **Bambu account** sign-in on the same page (email + password, or email only for Google/Apple accounts, then the emailed or authenticator code; keeps only `bambu.token`, `bambu.refresh_token`, `bambu.uid`). With the account the hub can watch through Bambu's MQTT broker when the LAN isn't reachable (always watch-only) and learns the serial and access code itself. Config `printers.printers[]` (`id`, `name`, `model`, optional `host`, `serial`, `file_url_prefix`) defaults to one printer `h2s`.

- `GET /v1/home` → `printers[]` summaries `{id, name, model, state, online, set_up, job, color}` for people with `appliances` and for children.
- `GET /v1/printers` → `{printers: [status]}`; `GET /v1/printers/{id}`. Status: `state` idle|preparing|printing|paused|done|failed|offline, `online`, `set_up`, `missing[]`, `problem`, `job {name, progress, minutes_left, finish_at (epoch s), layer, layers, stage}`, `temps {nozzle, bed, chamber: {current, target}}`, `speed` silent|standard|sport|ludicrous, `light`, `ams [{id, humidity, temp, drying, trays [{slot, index, empty, color, material, name, remain}]}]`, `active_slot`, `error`, `actions[]` (empty for children, who also get no temps), `control` full|watch, `via` lan|cloud|null, `watch_reason`.
- `GET /v1/printers/{id}/snapshot.jpg` (chamber camera, first frame takes a few seconds), `/mjpeg` (multipart, ~2 fps).
- Library: `GET /v1/printers/library` → `{models: [{id, title, bytes, added_at, plates [{index, seconds, grams, filaments [{id, type, color, grams}]}], thumbnail_url, prints}]}`; `POST` multipart `file` (sliced `.gcode.3mf` only; 400 `not_sliced` otherwise), `title`; `PATCH /library/{id}` `{title}`; `DELETE /library/{id}`; `GET /library/{id}/thumb.png`; `POST /library/{id}/ask` (a child asks the grown-ups).
- Actions (`POST /v1/actions`, target = printer id, cap `appliances`): `printer.control {command pause|resume|stop}` (stop is secure), `printer.light {on}`, `printer.speed {speed}`, `printer.temp {heater nozzle|bed|chamber, temp °C}` (max 350/120/65), `printer.print {model_id, plate, ams_mapping[], bed_leveling, timelapse}` (secure). Errors: 409 `printer_offline`, 409 `printer_busy`, 409 `watch_only`, 502 `upload_failed`.
- SSE `printer` event: the full status (no per-person trimming) whenever state, job, light or active spool changes. Push (cat `printer`) to whoever started the print when it finishes, fails or pauses with an error.
- App: `WorkshopView` (Destination `.workshop`), `PrintLibraryView` (`.printLibrary`), deep links `thomascreek://workshop`, `thomascreek://workshop/library`; `PrinterNowCard` on Home while a print is active.

## NAS: TrueNAS and its apps (2026-11-13)

Admin only (`systems` capability). The hub talks to TrueNAS's JSON-RPC WebSocket with the `truenas.api_key` vault entry; the app never sees it.

- `GET /v1/nas` -> `{system:{hostname,version,model,cpu,cores,memory_bytes,ecc,load[],uptime_s,os_update?}, pools:[{name,healthy,status,size_bytes,used_bytes,used_pct,fragmentation,scrub:{state running|paused|finished|none,pct,errors,ended?,secs_left?},vdevs:[{type,status,disks:[{disk,status,errors}]}]}], disks:[{name,model,serial,size_bytes,type,pool}], alerts:[{id,level,text,klass,at}], apps:{total,running,crashed:[names],updates}, services:[{service,title,running,enabled}], truenas_url}`
- `GET /v1/nas/apps` -> `{apps:[{name,state,custom,train,version,app_version,latest_version,update: "upgrade"|"images"|null,containers,links:{lan,remote},protected: "hub"|"remote"|null}]}`
- `GET /v1/nas/apps/{name}` -> the row plus `container_details:[{id,service,image,state,ports}]`, `volumes:[{source,destination}]`, `upgrade:{to,to_app_version,changelog}?`
- `GET /v1/nas/apps/{name}/logs?container=&tail=300` -> `{container, containers:[{id,service,state}], lines:[{t,text}]}` (TrueNAS `app.container_log_follow`, read for ~2 s)
- `GET /v1/nas/jobs/{id}` -> `{id,state,pct,description,error}`
- Actions (admin tier, Face ID): `nas.app {op start|stop|restart|update}` (target app; replies with `job`), `nas.update_all` (background, one at a time, skips protected apps, push at the end), `nas.alert` (target alert id), `nas.scrub {op start|pause|stop}` (target pool), `nas.snapshot` (target dataset), `nas.service {on}` (target cifs|nfs|ssh|ftp|iscsitarget|ups|snmp).
- Guards: the hub app (`thomascreek-hub`) can't be stopped; restart/update answers first, then redeploys itself. Apps carrying remote access (nginx-proxy-manager, tailscale, cloudflare-ddns) answer 409 `away_warning` when the caller is away; resend with `params.away_ok: true`.
- Config: `truenas.extra.links: {app: {lan, remote}}` overrides where "Open web page" goes; `truenas.extra.remote_apps` changes the remote list.
- Push: a newly crashed app sends a `systems` notice with ref `thomascreek://nas/app/<name>`. Deep links: `thomascreek://nas`, `/nas/apps`, `/nas/updates`, `/nas/app/<name>`.

## Weather (2026-11-13)

Hub: `hub/forecast.py`, `routes/weather.py`. Free sources only, fetched and cached by the hub, so the app only talks to the pinned hub.

- `GET /v1/weather?place=home|here|trip:<id>[&lat=&lon=]` (any signed-in person). `here` sends the phone's location rounded to 0.01°; within 25 miles of the house it answers as `home`. Trips appear in `places` from 7 days before they start until they end (TREK; the first stop or stay with coordinates).
  - `place {id, kind, name, lat, lon, country: US|DE|other, tz, spot?}`, `places [{id, kind, name, start_date, end_date, now}]`
  - `now {temp, feels, condition, symbol, code, is_day, humidity, dew_point, wind, wind_dir, gust, pressure (inHg), pressure_trend, visibility (mi), uv, uv_words, cloud, precip}` (°F, mph, inches everywhere)
  - `minutely [{t, precip}]` (15-minute steps, 2 h) and `next_hour` (one line); `hourly` 48 × `{t, temp, code, symbol, rain_chance, precip, wind, gust}`; `daily` 10 × `{date, high, low, symbol, condition, rain_chance, precip, snow, wind, gust, wind_dir, uv, sunrise, sunset, text_day?, text_night?}` (text from the NWS in the US). Times are the place's wall clock; use `tz`.
  - `summary` (NWS first period), `air {aqi, words}`, `alerts [WxAlert]`, `alerts_ok`, `sources [str]`, `updated`.
  - Grown-ups also: `outlook [{id, title, dates, temp {tone: warm|cold|even, words, prob}, precip {tone: wet|dry|even, ...}}]` (US: CPC 6–10, 8–14, month, 3-month at the point; elsewhere ECMWF seasonal by month), `outlook_source`, `radar {provider: nexrad|dwd|rainviewer, frames [{id, t, future}], max_zoom, step_min, attribution}`.
  - Children instead: `wear [{emoji, words}]`, alerts reduced to warnings `{id, event, level}`; no radar or outlook.
- `WxAlert {id, source: NWS|DWD, event, level: warning|watch|advisory|statement, severity, certainty, urgent, headline, headline_local (DWD German), area, sender, onset, ends, description, instruction, polygon [[lon, lat]]?}`.
- `GET /v1/weather/alerts/{id}`: one alert in full (grown-ups). 404 `expired` once it has ended.
- `GET /v1/weather/radar/{provider}/{frame}/{z}/{x}/{y}.png`: radar tile through the hub (disk cache, 3 h). 204 = no tile. Grown-ups only.
- `/v1/home` → `weather.alert {id, event, level}` and `weather.alert_count` when an official alert covers the house.
- Push: category `weather` (on by default per device). Per person in `/v1/alerts` prefs: `weather {warnings: true, watches: true, advisories: false, trips: true}`; works even when house alerts are off. Warnings ring (tornado, flash-flood, extreme-wind and other "urgent" ones also in quiet hours), the rest arrive quietly. Children never get them. `ref` = `thomascreek://weather/alert/<id>`. History kind `weather`.
- Smart card rule `wx_alert` (grown-ups): a warning (priority 95) or watch (70) for the house, `ref` to the alert.
- Deep links: `thomascreek://weather`, `/weather/alert/<id>`, `/weather/radar`, `/weather/outlook`.

## Thermostat history (2026-11-13)

- `GET /v1/climate/{entity_id}/history?range=6h|24h|3d|7d` (default 24h). 404 when the person can't see that thermostat on Home.
- Reply: `{entity_id, name, unit ("°F"|"°C"), range, start, end, points [{t, temp?, humidity?}], setpoints [{t, heat?, cool?, target?}], runs [{start, end, action: heating|cooling|fan|drying|…}], modes [{start, end, mode}], outdoor [{t, temp}], reports_action, summary {heating_s, cooling_s, fan_s, heating_cycles, cooling_cycles, temp_min?, temp_max?, temp_avg?, humidity_now?, humidity_min?, humidity_max?}}`.
- Times are Unix seconds. `temp: null` is a gap (thermostat unavailable). Set points are steps: each holds until the next. Auto holds `heat` (low) and `cool` (high); Heat/Cool hold one; `target` only for an Auto thermostat with a single set point. `reports_action: false` means the thermostat never says when it runs, so there are no run bands. The line and set points run on to `end`.
- `climate.set` also takes `{target_low, target_high}` (both together, low < high) for a thermostat in Auto (`heat_cool`); sent as HA `set_temperature` with `target_temp_low`/`target_temp_high`. Children's values are clamped like `target_temp`.
- App: `Destination.climateHistory(id)` (`ClimateHistoryView`), embeddable `ClimateHistorySection(deviceID:)`; deep link `thomascreek://climate/<entity_id>`.

## TVs and boxes: find and add for the remote (2026-11-13)

Adults and admins (`remotes.setup`). Pairing runs through Home Assistant's own setup flows (`androidtv_remote`, `apple_tv`), so a TV added from one phone is on everyone's remote; nobody pairs on their own device. The hub's Home Assistant token must belong to an HA admin (else `409 ha_admin_needed`).

- `GET /v1/remotes[?fresh=1]` → `{added:[{id, kind: android|apple, name, entity_id?, room?, state?, working, disabled}], found:[{id, kind, name, host?, model?, via: home_assistant|network}], scanned_at}`. `found` merges Home Assistant's discovery (`flow:<id>`) with a 2.5 s mDNS query from the hub (`net:<kind>:<ip>`), minus anything already added. The scan is cached 20 s; `fresh=1` rescans.
- `POST /v1/remotes/pair {id, name?}`: `id` is a `found` id, or `host:<android|apple>:<address>` to add by address. Returns a step.
- `POST /v1/remotes/pair/{flow_id} {pin?}` answers a step; `DELETE /v1/remotes/pair/{flow_id}` stops.
- Step: `{flow_id, kind, name, state: form|done|failed, step?, needs_pin?, show_code?, message, error?, entry_id?, reason?}`. `message` is ready to show. `needs_pin`: Android TV shows 6 letters and numbers, Apple TV 4 digits (sometimes once per feature, so expect more than one PIN step). `show_code`: a code to type on the Apple TV, then Continue. `done` refreshes the home model and sends SSE `{type: "remotes"}`; the new TV then appears in `/v1/home` with its remote buttons.
- Deep link `thomascreek://tvs`. Activity: `remotes.pair_start`, `remotes.add`.

## Links between things (every reference opens its object)

- Anything the app mentions opens when tapped: temperatures and weather lines open Weather (a trip's forecast with `thomascreek://weather/place/trip:<id>`), indoor temperatures open Climate, devices open their own page, names open the person. A tapped reference opens on top of the current screen (`Router.follow`), so Back returns to it; deep links from pushes still switch tabs (`Router.open`).
- New deep links: `thomascreek://device/<entity id>` (device page; kids get My Room), `thomascreek://person/<person id>` (grown-ups), `thomascreek://weather/place/<home|here|trip:<id>>`.
- Smart cards: every card carries `ref`, its subject. The hub fills it from the one device a card names (garage, lock, habit), else the card's first `open` button, else a per-rule screen (`smart.RULE_REFS`). The app opens `ref` when the card itself is tapped; its buttons still act.
- Activity rows open their `target` (a device, camera or room the app knows); `person_id` opens the person.
- Ask Thomas Creek writes Markdown links with these URLs for the things it names; the app routes them through `Router.follow` and closes the panel.
## House layout, TVs and appliances (2026-11-13)

- `GET /v1/home` lists one media player per real TV or box (the hub drops Cast/DLNA/AirPlay copies,
  wall tablets, voice assistants, laptops, Plex sessions and players dead for 3+ days, and keeps the
  remote-capable copy, borrowing a room from another copy). A child's own TV always stays.
- `appliances` (people with `appliances`): one card per real appliance: `{id, type (oven|fridge|dishwasher|
  washer|dryer|microwave|vacuum|water_heater|appliance), name, symbol, room_id, status, running, available,
  door_open, parts:[{id,name,value}], oven_id?, maker, model}`. Temperature sensors are not appliances; GE
  fridge/freezer water heaters are no longer "shower" devices. Tap an oven card to open `.kitchen(oven_id)`.
- Rooms carry `scenes: [{id, name, icon}]` (run with `scene.run`) and `device_ids` in the room's saved order.
- Admin only (`home.layout`): `GET /v1/home/layout` (hidden, rooms, cleanup = dead 3+ days),
  `POST /v1/home/hidden {ids, hidden}` (entity ids, or `device:<id>` for appliances),
  `GET|PUT /v1/rooms/{id}/layout` (`{name?, symbol?, hidden?, order?, add?, remove?}`; add/remove move
  devices between rooms). Each change sends a `snapshot` event so every phone reloads. Hidden devices still
  work for actions, modes and Ask; they're only left out of lists and search.
- App: `HideForEveryoneButton(deviceID:)` (drop into any menu or device page), `RoomEditView`, `HiddenDevicesView`
  (House › Hidden devices), `AppliancesView`.

## Full TV remote, watch anywhere, and boxes on TVs (2026-11-13)

- `state.remote` on media devices: `{platform, keys[], apps[{id,name,kid}], inputs[], text, app}`. Keys are only the
  ones that TV's integration supports. `apps` uses shared ids (youtube, netflix, plex, disney_plus, ...) on every
  kind of TV, and unknown apps come through as `src:<name>`.
- `media.command` params: `command` is one of the remote keys (up/down/left/right/select/back/home/menu/play_pause/
  ch_up/ch_down/input/guide/...), `app` + `app: <id>`, `text` + `text: <string>` (Google TV), `source` + `source`.
  Children can only open kids' apps (403 with ask_parent). The hub wakes the TV first, reconnects a TV that dropped
  off, retries once, and returns a plain-language 502 when it still can't reach it.
- `GET /v1/media/watch`: `{apps:[{id,name,kid,screens:[{entity_id,name,room,on}]}]}`, for "watch X on Y".
- `POST /v1/media/play` on a TV with a Plex app returns `{pending:true, message}`. The hub opens Plex, finds the TV's
  Plex player, picks followable tracks and starts the title. The outcome arrives as event `watch`
  `{ok, message, tracks?}`.
- `POST /v1/media/{id}/language` `{target_device_id?}` → `{ok, message, audio, subtitles, switched_now}`.
- `GET /v1/media/hookups` (adults) → `{screens:[{entity_id,name,platform,inputs,plugged_into,boxes}]}`.
  `PUT /v1/media/hookups/{box}` `{display_entity_id|null, input|null}` says which TV a box is plugged into and on
  which input. When the box is used, the hub turns that TV on and switches its input, sends volume to the TV, and
  turns both on or off together. The box's state carries `plugged_into {entity_id,name,input}`.

## TV guide and play on a house TV (2026-11-13)

The source of truth is `server/docs/API.md` (Thomas Creek TV). Shapes the iOS app now calls:

- `GET /v1/tv/guide` → `{channels:[{key,name,number,logo,now,next,programmes}]}`. Programme: `{start,end,title,subtitle,desc,icon,catchup}`.
- `GET /v1/tv/series`, `GET /v1/tv/series/<id>`, `GET /v1/tv/movies`, `GET /v1/tv/movies/<id>`, `GET /v1/tv/library/search?q=`.
- `GET /v1/tv/players` → `{tvs:[{id,name,room,room_name,profile,online,playing,last_command}]}`. `id` is the TV install id, the `target` of `tv.play`.
- `POST /v1/actions` `{action:"tv.play", target:<install id>, params}`. Params: `{channel_key, title?}` live; `{kind:"catchup", channel_key, start, resume?}`; `{movie_id, resume?}`; `{episode_id, series_id, resume?}`. Response adds `command_id` when the hub actually delivered it (`dry_run` means practice mode, nothing was sent).
- SSE `tv`: `{id,name,room,playing,command?:{id,ok,error}}`. The app toasts `command`. A failure is `ok: false` and `error`.

Kids only get kids' rows from the hub. The guide screen does not filter a second time.

App: Media and the kids' Watch tab open `TVGuideView` (`Destination.tvGuide`, `thomascreek://guide`). A row opens `PlayOnTVSheet`, which lists `/v1/tv/players` and sends `tv.play`. Series and movie pages are `TVSeriesView` / `TVMovieView`.

Follow-ups, not in this build: resume slider, a full programme grid (the guide shows now and next, not every row in the window), artwork caching, and waking the TV through `media.command` before `tv.play`. The Android TV app still has to enroll, take the token, and apply `tv_command`.

## Universal search (2026-11-13)

`GET /v1/titles/search?q=` is the one title search (Plex, IPTV channels, guide, VOD, series, and TMDB through Jellyseerr). The house search (`GET /v1/search`) is unchanged and still runs beside it.

Each `titles[]` card is one film or show. `offers[].label` is the line to show (`In Plex (4K)`, `IPTV VOD`, `Airs Tue 8pm on ESPN`, `Catch-up until Fri`). `offers[].params` is ready for `tv.play`. `offers[].plex_id` opens the existing Plex play sheet (`PlayOnSheet` / `KidPlaySheet`: play on a TV through `/v1/media/play`, or download to the phone). `offers[].series_id` opens `Destination.tvSeries`.

`request.status` is `waiting`, `requested`, `downloading`, `available`, or null. `can_request` shows Request. `ask_parent` means the button says "Ask a grown-up" and `POST /v1/titles/request {media_id}` waits for `POST /v1/requests/{id}/approve` instead of calling Jellyseerr itself.

The search sheet shows these cards above the other groups (`TitleResultCard`). The TV app uses the same GET.
