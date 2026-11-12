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
- `GET /v1/calendar?days=7` -> `{events:[{id?,title,start,end?,who?,all_day?}]}`
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
