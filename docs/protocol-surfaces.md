# Protocol Surfaces

elastik doesn't build clients. It parasitizes existing ones.

Every device already has a browser, a file manager, a calendar, a
contacts app, a podcast player, a TV. These are the clients. elastik
speaks their protocols. They don't know elastik exists.

## Implemented

| Protocol | Client | What it sees | Status |
|----------|--------|-------------|--------|
| HTTP/HTML | Browser | Interactive UI, images, video, PDF | Done |
| WebDAV | Finder, Explorer, VS Code | Files and folders | Done (v2.9) |
| MCP | Claude, Cursor, Claude Code | Tool calls | Done (v2.8) |
| HTTP/JSON | curl, scripts, Shortcuts, Tasker | API endpoints | Done |
| EBP | Microcontrollers, UART devices | Byte stream | Done |

## Planned

### CalDAV — calendar apps

iPhone Calendar, Google Calendar, Thunderbird connect natively.
World stores iCalendar (.ics) format. Phone syncs bidirectionally.

Not a file. An event. WebDAV can store .ics files but calendar apps
won't sync them — they need the CalDAV protocol (REPORT method,
time-range queries, ctag/etag for sync).

```
GET  /caldav/schedule/       → PROPFIND → list events
PUT  /caldav/schedule/uid.ics → create/update event
```

World `schedule` stores events. Phone calendar displays them.
AI writes events via MCP. Phone shows them in the native calendar.
No app needed.

### CardDAV — contacts apps

iPhone Contacts, Android Contacts connect natively.
World stores vCard (.vcf) format.

```
GET  /carddav/contacts/       → PROPFIND → list contacts
PUT  /carddav/contacts/uid.vcf → create/update contact
```

AI manages your contacts. Phone displays them natively.
"Add the person I just met" → AI writes vCard → phone syncs.

### RSS/Atom — feed readers

Any RSS reader, any podcast app. Subscribe to a world's change history.

```
GET /rss/{world} → Atom feed of recent stage changes
```

HMAC chain is already an append-only log. Each write = a feed entry.
15 lines: SELECT recent events, format as Atom XML, return.

Others subscribe to your world. Updates push through RSS infrastructure
that's been running for 20 years. No WebSocket. No polling. Feed
readers handle it.

Podcast variant: world stores audio BLOB (ext=mp3). RSS feed has
enclosure tags. Podcast apps pick it up. Your universe.db is a
podcast host.

### DLNA/UPnP — TVs and speakers

Living room TV, Sonos, any media device on the network.

```
SSDP discovery → "I'm an elastik media server"
SOAP browse    → list worlds with media ext (mp4, mp3, jpg)
HTTP stream    → GET /{name}/raw → Content-Type from ext
```

TV browses elastik like a media library. Plays video from /raw.
No app. No cast. No Chromecast. TV's built-in DLNA client does it.

Photos on the TV: world `photos/vacation` with ext=jpg → TV slideshow.
Music: world `music/playlist` → speaker plays from /raw.

## Architecture

All surfaces share one storage layer:

```
CalDAV  ─┐
CardDAV ─┤
RSS     ─┤
DLNA    ─┼──→ conn(name) → universe.db → stage_html/ext
WebDAV  ─┤
MCP     ─┤
HTTP    ─┤
Browser ─┘
```

Each protocol is a plugin. Install it, the surface appears. Uninstall
it, the surface disappears. Data stays in universe.db regardless.

Same world, different protocol, different client, different experience.
The calendar app sees events. The browser sees a rendered schedule.
The TV sees a video. The file manager sees a file. RSS readers see
a feed. All from the same row in stage_meta.

### Apple native surfaces (via Content-Type alone)

No protocol implementation needed. Just serve the right bytes with
the right Content-Type from `/raw`. iOS does the rest.

**Apple Wallet (.pkpass)**
`application/vnd.apple.pkpass`

Store a .pkpass BLOB in a world. iPhone visits `/boarding-pass/raw` →
Safari sees the MIME type → "Add to Wallet" dialog. Boarding passes,
event tickets, loyalty cards, coupons — all from a URL.

.pkpass is a signed ZIP (pass.json + images). Creating one requires
an Apple developer certificate. Serving one requires nothing —
just bytes + Content-Type.

```
POST /boarding-pass/write?ext=pkpass  body: [signed .pkpass bytes]
GET  /boarding-pass/raw → Content-Type: application/vnd.apple.pkpass
→ iPhone: "Add to Apple Wallet?"
```

**iOS Configuration Profile (.mobileconfig)**
`application/x-apple-aspen-config`

Store a .mobileconfig XML in a world. iPhone visits `/setup/raw` →
"Install Profile" dialog. Auto-configure WiFi, VPN, email, calendar
accounts, certificates — from a URL.

```
POST /office-wifi/write?ext=mobileconfig  body: [profile XML]
GET  /office-wifi/raw → Content-Type: application/x-apple-aspen-config
→ iPhone: "Install Profile?"
```

No signing required (unsigned profiles show a warning but install).
IT departments use this for device enrollment. You use it to configure
your own phone from elastik.

These aren't protocol surfaces — they're MIME type surfaces. The
protocol is plain HTTP. The magic is the Content-Type header that
triggers native iOS behavior. `/raw` already does this. Just needs
the MIME types in `_CT`.

## Selection criteria

A protocol surface is worth adding only if:

1. **Existing clients**: billions of devices already speak it
2. **Not replaceable**: another existing surface can't do the same thing
3. **Plugin-sized**: implementable in one .py file, <200 lines
4. **No new dependencies**: stdlib HTTP/XML handling is enough

CalDAV, CardDAV, RSS, and DLNA all pass. Each unlocks a class of
device that no other surface reaches.
