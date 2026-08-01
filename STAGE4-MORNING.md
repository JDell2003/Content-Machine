# Stage 4 — the morning list

Everything that could be done without you is done. What's left is two things
only you can do, because they live behind your Facebook login.

**Auto-posting is NOT live yet.** It cannot be until you do steps 2 and 4 below.
Budget about 10 minutes.

---

## Where things stand

| | |
|---|---|
| Postiz | **running**, http://localhost:5000, HTTP 200 |
| postiz-postgres | up, healthy |
| postiz-redis | up, healthy |
| Docker data | on **D:\DockerData** (8.5 GB), C: has 10.7 GB free |
| Approve & Schedule | **built and wired**, waiting on credentials |
| Post registry | **built** — media id is captured at publish for UP3 |

### Resource footprint (measured, idle)

```
postiz            0.45% CPU    1.13 GB RAM
postiz-postgres   0.00% CPU      49 MB
postiz-redis      0.13% CPU       8 MB
                             ~1.19 GB total
```

Negligible CPU at idle. It will spike briefly while uploading a clip.

**No Cloudflare Tunnel was needed.** Postiz is bound to `127.0.0.1:5000`. Meta's
redirect only has to be reachable by the browser doing the connecting — which is
this PC — and publishing afterwards is outbound. Nothing needs to reach in from
the internet, so nothing is exposed to it. One less moving part, still $0.

---

## 1. Create the Postiz account (1 min, local only)

Open **http://localhost:5000** and register. First account owns the instance.
`DISABLE_REGISTRATION` is false so this works; consider flipping it to `true`
afterwards so the box can't grow a second account.

---

## 2. Meta console — the redirect URI and scopes  ← YOUR CLICKS

**developers.facebook.com → your app (ID 1750415476382679)**

### The redirect URI — exact, read off the running container

```
http://localhost:5000/integrations/social/instagram
```

Use `instagram`, **not** `instagram-standalone`. Standalone is for Instagram
Login without a Facebook Page; yours goes through the RiseForIt Page, which is
the `instagram` provider.

### Where to paste it

**Facebook Login for Business → Settings → Valid OAuth Redirect URIs**

### Use cases and permissions

The current console is the use-case dashboard, so the old flat scope list is now
grouped. Add the use case:

- **Instagram → "Manage messaging and content on Instagram"** (or whatever the
  console labels the Instagram publishing use case for your app type)

Then under **Customise → Permissions**, make sure these are added:

| permission | why |
|---|---|
| `instagram_basic` | read the connected IG account |
| `instagram_content_publish` | **the one that actually posts** |
| `pages_show_list` | find the RiseForIt Page |
| `pages_read_engagement` | read the Page↔IG link |
| `business_management` | resolve the business asset |
| `instagram_manage_insights` | UP3 metrics later |
| `pages_manage_posts` | only if you also post to the Page itself |

If a permission shows "Requires App Review", that's expected while unreviewed —
it still works for accounts with a **role on the app**, which is what step 4
relies on.

---

## 3. Paste the credentials  ← YOUR CLICKS

In Postiz: **Settings → Public API** → generate a key.

Then in `D:\ContentMachine\.env`:

```
CM_POSTIZ_API_KEY=<the key from Postiz>
CM_POSTIZ_INTEGRATION_ID=<the Instagram integration id, after step 4>
CM_SCHEDULE_DEFAULT=1        # makes the Schedule toggle default ON
```

`postiz/.env` already has your App ID and Secret. **Rotate that secret** — it
went through a chat transcript.

Restart the Content Machine after editing (`run.bat`).

---

## 4. Connect Instagram  ← YOUR CLICKS

In Postiz: **Add Channel → Instagram** → it opens Facebook → authorise → pick
the **RiseForIt Page** → pick **jason_odell_coaching**.

If it refuses: your app is in Live mode, but Instagram publishing often still
wants Business Verification. The fast way past that is **App Roles → Roles →
Add Instagram Tester**, invite `jason_odell_coaching`, then accept the invite
inside the Instagram app (Settings → Website Permissions → Tester Invites).

Once connected, copy the **integration id** into `CM_POSTIZ_INTEGRATION_ID`.

---

## 5. Acceptance test

1. Content Machine → **Swipe**
2. The **Approve & schedule** toggle should now be enabled (it greys itself out
   and says why when it can't work — it never pretends)
3. Swipe right on one clip
4. It goes to the **next open calendar slot**, not "now" — the governor decides.
   For a 10-minute test, add a slot time 10 minutes out on the Calendar tab.
5. Check `/patterns` — the post should be listed with its **media id**

If the push fails, the clip is still approved and downloadable. That ordering is
deliberate: the approval commits first, so a Postiz error never costs you the
clip.

---

## What is deliberately NOT automated

Nothing here publishes on its own without you approving a clip. The cadence
governor is a ceiling, not a quota: surplus approvals extend into future days
rather than flooding today, and an empty slot stays empty. There is no backfill
prompt anywhere, by design.
