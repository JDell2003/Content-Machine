# STAGE 4 — Postiz auto-scheduling

Status: **researched + costed, NOT deployed.** You asked for hosting cost before
enabling, so nothing has been created on Railway yet.

Sequencing note up front: this is the exhaust pipe, and the engine (Stages 1–3)
isn't built. Nothing can be tested end-to-end until clips exist. The parts below
that are useful now are the cost decision and the Meta setup — both are on your
side and both take real calendar time (Meta especially), so doing them in
parallel with my build work is the right overlap.

---

## 1. Hosting cost on Railway

Two templates exist. **Take the lightweight one.**

| Template | Services | Realistic cost |
|---|---|---|
| **postiz-app v2.11.3** (recommended) | Postiz + Postgres + Redis | **$5–12/mo** |
| postiz-with-temporal | 8 services incl. Temporal + Elasticsearch | **$20–35/mo** |

Railway's model (verified July 2026):
- Hobby: **$5/mo base, includes $5 usage credit**
- Pro: $20/mo base, includes $20 credit
- Memory ~$0.334/GB/month · vCPU ~$0.668/vCPU/month · volumes ~$0.156/GB/month ·
  egress $0.05/GB

Estimate for the 3-service stack: ~1.5–2.2 GB RAM total (~$0.50–0.75), ~0.2–0.5
vCPU (~$0.13–0.33), 10 GB volumes (~$1.55), egress trivial (100 MB clips × 30/mo
= 3 GB = $0.15). That lands **inside the $5 Hobby credit on paper**.

### The catch you need to check
**RiseForIt already runs in your Railway workspace and is already burning that
credit.** The $5 is per-workspace, not per-project. So Postiz is incremental
usage on top of what RiseForIt already consumes — you will likely go past the
included credit and pay overage. Check **Railway dashboard → Usage** for your
current monthly burn before you decide. If RiseForIt already exceeds $5, budget
the full $5–12 as net-new.

Alternative worth considering: Postiz self-hosts fine on the Content Machine PC
itself via Docker (it's already always-on, already has the disk). That's $0/mo
and keeps clips off the cloud entirely — but it needs a public HTTPS callback URL
for Meta OAuth, which is the one thing a LAN-only box can't provide. Railway
solves exactly that one problem. Your call.

---

## 2. Meta side — exact clicks

Do these in order. Steps 1–2 must be done before 3 or the app can't see the account.

### Step 1 — Instagram to Professional (Business or Creator)
1. Instagram app → your profile → **☰** (top right) → **Settings and activity**
2. Scroll to **Account type and tools** → **Switch to professional account**
3. Pick a category → choose **Business** (Creator also works; Business is safer
   for publishing API access)
4. When it offers to connect a Facebook Page, **say yes** — you need this for the
   Facebook Business path in step 4.

### Step 2 — Facebook Page linked
If you don't have a Page: facebook.com → **Menu (☰)** → **Pages** → **Create new Page**
→ name + category → **Create**.

To confirm the link: Instagram → Settings and activity → **Sharing and remixes**
(or **Linked accounts**) → **Facebook** → the Page should be listed.

### Step 3 — Create the Meta app
1. Go to **developers.facebook.com** → log in → **My Apps** → **Create App**
2. **App type: "Other"** → Next
3. **Business type: "Business"** → Next
4. Name it (e.g. "Content Machine Poster"), your email → **Create app**
5. Left sidebar → **App settings → Basic**. Copy **App ID** and **App Secret**
   (click Show). You'll paste both into Postiz.
6. Still on Basic: add a **Privacy Policy URL** (required later for review — any
   public URL works for dev mode) and save.

### Step 4 — Add Instagram + permissions
1. App dashboard → **Add products** → add **Instagram** (Instagram Graph API)
2. **App roles → Roles**: confirm you're **Administrator**. Add your own IG user
   as a **Tester** if prompted.
3. **App review → Permissions and features** → request Advanced Access for:
   - `instagram_basic`
   - `instagram_content_publish`
   - `instagram_manage_comments`
   - `instagram_manage_insights`
   - `pages_show_list`
   - `pages_read_engagement`
   - `business_management`

### The App Review question — read this
Meta docs say these scopes need App Review. **For your own account you almost
certainly don't need it.** In **Development mode**, anyone with a *role on the
app* (Administrator / Developer / Tester) can use advanced permissions. Since
you're the admin posting to your own IG, dev mode is enough.

Full App Review (business verification, screencast, days-to-weeks turnaround)
only becomes necessary when you post on behalf of accounts that aren't yours —
i.e. the trainer-facing version, which is already gated behind your first closed
client. Don't burn a week on review now.

### Step 5 — OAuth redirect URI
In the Meta app → **Facebook Login → Settings** (and the Instagram product
settings), add **Valid OAuth Redirect URI**, exactly:

```
https://<your-postiz-domain>/integrations/social/instagram
```

Use the Railway-generated domain (e.g. `postiz-production-xxxx.up.railway.app`).
It must be **https** and match character-for-character.

If you use the standalone (no Facebook Page) path instead, the URI is
`.../integrations/social/instagram-standalone` and it requires a *professional*
IG account.

### Step 6 — Connect in Postiz
1. Postiz → **Settings → Add channel → Instagram**
2. Postiz env vars (Railway → Postiz service → Variables):
   ```
   FACEBOOK_APP_ID=<App ID from step 3>
   FACEBOOK_APP_SECRET=<App Secret from step 3>
   ```
   (Standalone path uses `INSTAGRAM_APP_ID` / `INSTAGRAM_APP_SECRET` instead.)
3. Redeploy the service so the vars load, then click through the OAuth prompt and
   pick the IG account + Page.

---

## 3. Pipeline wiring (my side, not yet built)

Design, for when the core pipeline exists:

- **Approval page** gains a per-clip **Schedule** toggle. ON → push to Postiz.
  OFF → download-only (current behaviour).
- On Approve with Schedule ON: POST the clip file + caption + hashtags to Postiz,
  target **next open slot** in the posting calendar, editable afterwards in
  Postiz.
- Postiz API: `POST /public/v1/posts` with a bearer token from Postiz →
  Settings → Public API. Upload the media first, then reference the returned id.
- Config to add to `.env`:
  ```
  CM_POSTIZ_URL=https://<postiz-domain>
  CM_POSTIZ_API_KEY=
  CM_POSTIZ_DEFAULT_SLOT=next-open
  CM_SCHEDULE_DEFAULT=0
  ```
- Guard rails: refuse to send anything over **100 MB**, refuse raw source files,
  log every push with clip id + response so a failed schedule is visible on the
  status page rather than silent.

---

## 4. Future: Instagram Insights API (for Upgrade Pass 3)

UP3 has you thumb-entering views/likes/comments/shares/saves into
`performance.jsonl`. That schema is deliberately the same shape the API returns,
so auto-pull is a drop-in later with **zero schema change**.

- Endpoint: `GET /{ig-media-id}/insights?metric=...` on the Instagram Graph API
- Metrics: `impressions`, `reach`, `likes`, `comments`, `shares`, `saved`,
  `plays`, `total_interactions`; Reels also expose `ig_reels_avg_watch_time` and
  `ig_reels_video_view_total_time` — that's your avg-watch-% column
- Needs `instagram_manage_insights` (already in the scope list above) and the
  media id, which Postiz returns on publish — **store it in the post registry at
  publish time** or you'll have nothing to query later
- Requirements: IG Business/Creator + linked Page (done in steps 1–2). Posting on
  your own behalf stays dev-mode-legal; App Review only for other people's
  accounts.
- Practical note: insights are only available for media published *via the API*.
  Clips you post manually from the phone won't be queryable — which is the real
  argument for turning the Schedule toggle on once Postiz is live.
