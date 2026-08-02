# Stage 4 — everything on port 3000, no Postiz

Postiz is gone. What replaced it, why, and the three things only you can do.

---

## What changed and why

**Postiz's backend was crashed**, not misconfigured. The current image needs a
**Temporal** workflow service my compose file never included:

```
connect ECONNREFUSED ::1:7233
nginx: connect() failed while connecting to upstream 127.0.0.1:3000
```

That is what "Could not add provider" was. The backend never started, so the
frontend had nothing to talk to.

**And a correction to what I told you earlier.** I said no tunnel was needed. I
checked that OAuth would work over localhost and stopped there. But Instagram's
publishing API does not accept a file upload — you give it a `video_url` and
**Meta's servers download it**. A localhost-only server cannot serve that. So
publishing would have failed at the acceptance test with Postiz working
perfectly. That was my mistake.

Given both, Postiz was ~1.2 GB of RAM and four containers wrapping two HTTP
calls. It is now removed and Instagram is called directly:

| before | now |
|---|---|
| Content Machine :3000 + Postiz :5000 | **:3000 only** |
| postiz + postgres + redis + temporal | none |
| ~1.2 GB RAM idle | 0 |
| two addresses to remember | one |

---

## The one thing that must be public, and how little it is

Instagram has to fetch the video, so exactly one route is exposed:

```
/share/<32-random-characters>
```

* random token, maps to ONE file, expires in 30 minutes, revoked the moment
  publishing finishes
* unknown and expired tokens both 404 — indistinguishable from outside
* the Cloudflare tunnel config forwards **only** `/share/*`. Every other path is
  refused at Cloudflare's edge and never reaches this machine.

Verified just now: `/share/bogus` -> 404, `/api/jobs` -> 401 without a session.

---

## 1. Cloudflare tunnel  <- YOUR CLICKS  (~5 min, free)

You need a domain on Cloudflare (a spare one is fine; it does not have to be
riseforit.com).

```powershell
winget install --id Cloudflare.cloudflared    # or download the .exe
cloudflared tunnel login
cloudflared tunnel create contentmachine
cloudflared tunnel route dns contentmachine clips.<your-domain>
```

Then edit `tunnel/config.yml` — replace the three `REPLACE_WITH_*` values with
the tunnel UUID (printed by `create`) and your hostname. Run it:

```powershell
cloudflared tunnel --config D:\ContentMachine	unnel\config.yml run
```

---

## 2. Instagram credentials  <- YOUR CLICKS

You need two values. Easiest route is the Graph API Explorer
(developers.facebook.com/tools/explorer):

1. Pick your app (**1750415476382679**), then **Generate Access Token** with:
   `instagram_basic`, `instagram_content_publish`, `pages_show_list`,
   `pages_read_engagement`, `business_management`, `instagram_manage_insights`
2. Query `me/accounts` -> find the RiseForIt Page -> copy its **id**
3. Query `{page-id}?fields=instagram_business_account` -> that **id** is your
   IG user id
4. **Exchange for a long-lived token** (the short one dies in ~1 hour):
   `GET /oauth/access_token?grant_type=fb_exchange_token&client_id=<APP_ID>
   &client_secret=<APP_SECRET>&fb_exchange_token=<SHORT_TOKEN>`

No redirect URI and no OAuth handshake is needed any more — that was a Postiz
requirement, not an Instagram one.

---

## 3. Paste into `D:\ContentMachine\.env`

```
CM_IG_USER_ID=<the instagram_business_account id>
CM_IG_ACCESS_TOKEN=<the LONG-LIVED token>
CM_PUBLIC_HOST=clips.<your-domain>
CM_SCHEDULE_DEFAULT=1
```

Restart with `run.bat`. Also still worth doing: **rotate the App Secret** in
`postiz/.env` — it went through a chat transcript.

---

## 4. Acceptance test

1. **Swipe** -> the "Approve & schedule" toggle enables itself once all three
   values are set. Until then it greys out and names exactly what is missing.
2. Swipe right on one clip.
3. It mints a share link, hands Instagram the URL, waits for Meta to finish
   transcoding, publishes, then revokes the link.
4. `/patterns` should list the post with its **media id** and permalink.

If the publish fails, the clip is still approved and downloadable. The approval
commits first, separately, on purpose.

---

## Honest status

**Auto-posting is not live.** Steps 1-3 need your hands: a Cloudflare account,
a Facebook login, and pasting three values. Nothing in the code can do those.

Everything on this side is built and verified: publish path, media checks
against Meta's limits, share tokens, registry capture of the media id, and the
insights pull for UP3.
