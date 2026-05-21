# LINE Messaging API setup

You need a LINE Messaging API channel + the target userId (or groupId)
to push news bubbles to. This takes ~10 minutes.

## 1. Create the channel

1. Go to https://developers.line.biz/console/ and sign in with your LINE account.
2. **Create a new provider** (any name; e.g. `XAU News`). This is just a
   namespace — you can put many channels under one provider.
3. Inside the provider, **Create a new channel** → choose
   **Messaging API**.
4. Fill in the basic info:
   - **Channel name:** anything (this is the bot name your users see)
   - **Channel description:** anything
   - **Category / Subcategory:** any reasonable pick
   - **Region:** Thailand (or wherever you are)
5. Agree to the terms and click **Create**.

## 2. Get the channel access token

1. Open the new channel.
2. Go to the **Messaging API** tab.
3. Scroll down to **Channel access token**.
4. Click **Issue** (the first time) or **Reissue**.
5. Copy the long token — this is your `LINE_CHANNEL_TOKEN`.

## 3. Disable webhook + auto-replies

The pipeline pushes only — it doesn't receive messages. So:

1. **Messaging API** tab → **Webhook URL** → leave blank, **disable**.
2. **Auto-reply messages** → **Disable**.
3. **Greeting message** → optional; disable if you want a silent bot.

## 4. Add the bot as a friend

You need to be a friend of the bot for it to push to your personal
LINE.

1. **Messaging API** tab → scroll to **QR code**.
2. Open LINE on your phone → **Add Friends** → **QR Code** → scan it.
3. The bot will appear in your friend list.

## 5. Get your userId (the push target)

LINE doesn't expose your userId in the app. Two ways to get it:

### Method A — quick reply via webhook (one-time)

Temporarily enable webhook, send any message to the bot, capture the
userId from the webhook payload, then disable webhook again. Tedious.

### Method B — broadcast then check delivery log

1. Open https://manager.line.biz/account/<your_channel_id>/dashboard
2. Send any **broadcast** message from the UI.
3. Open the channel insights — your userId appears in the recipient
   list. Copy it. Format: `U` + 32 hex chars.

This becomes `LINE_NEWS_TARGET` (and `LINE_HEALTH_TARGET` — usually
the same userId so health alerts land in the same chat).

### Method C — use a 1-line Python webhook server locally

```python
# pip install fastapi uvicorn
from fastapi import FastAPI, Request
app = FastAPI()
@app.post("/webhook")
async def w(r: Request):
    body = await r.json()
    print(body)
    return {"status": "ok"}
# uvicorn main:app --host 0.0.0.0 --port 8000
# then ngrok http 8000 and paste the public URL into channel webhook
```

Send any message to the bot — your userId prints in the terminal.

## 6. Group chat target (optional)

To push to a LINE group instead of your personal chat:

1. Invite the bot into the group.
2. Have the bot capture the **groupId** the same way (method A/C above).
3. Use the groupId (`C` + 32 hex chars) as `LINE_NEWS_TARGET`.

## 7. Free-tier quota

- **500 push messages/month** on the free Messaging API plan.
- The pipeline normally sends 30–80 pushes/day (3 digest slots + 2
  calendar pushes + occasional Breaking/Alert + health). That's
  ~1000–2400/month, which **exceeds the free tier**.
- Options:
  - Upgrade to **Light plan** (~150 THB/month, 15,000 messages)
  - Disable some workflows you don't need (e.g. drop one digest slot)
  - Use a group chat (group pushes count as one regardless of group size)

## 8. Verify

```bash
# In your local repo, with .env populated:
python -c "
from src.line_client import LineClient
from src.line_flex import health_bubble
import os
line = LineClient.from_env()
bubble = health_bubble([('_pipeline_heartbeat', 'watchdog_silence')])
resp = line.push_flex(os.environ['LINE_HEALTH_TARGET'], 'test', bubble)
print(resp)
"
```

If it prints `{'status': 200, ...}` you're good.
