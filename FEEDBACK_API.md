# Bucky feedback API: what the CRM sends and what Bucky returns

**For:** the bucketlistt CRM team
**Status:** built and running on our server, built from branch `feat/ai-suggested-prompt-changes` (not yet merged to `main`). This is the final contract: it answers the questions in your proposal and replaces it. Updated 2026-11-18.

**Base URL:** the same host you already use for `/api/v2/chat` (`{BUCKY_API_URL}`). Nothing new to configure except the new path and the stored `response_id`.

## 1. What changes for you (summary)

| # | What | Required? |
|---|---|---|
| 1 | Call `POST {BUCKY_API_URL}/api/v2/feedback` once for every feedback item a super admin **approved**. | **Yes** |
| 2 | Read the new `response_id` field in every `/api/v2/chat` response, **store it on the message**, and send it back as `flagged.response_id`. | **Yes**, starts now: without it we cannot tell which prompt version produced a reply |
| 3 | Retry on network error, timeout, 5xx and 409 (your 2s / 5s / 10s, then hourly for a day). Do **not** retry on 401 or 422. | **Yes** |
| 4 | Status callback from Bucky to the CRM ("applied" / "won't apply"). | **No: not built yet.** See section 8. |

Nothing else changes. `/api/v2/chat` only gains one field.

## 2. Our answers to your five questions

1. **Shape:** your proposed body is accepted as it is (section 4). Keep the transcript in your own format.
2. **Name and phone:** please keep sending them; Bucky **masks the phone and drops the name before storing** (`918860270303` becomes `91886*****03`, name removed). You do not need to mask on your side, but you may.
3. **Size:** 500 messages / 1 MB is fine. Over either limit we answer 422 (section 5). Send the latest 500 messages up to the flagged one and set `"truncated": true`.
4. **How it is used:** approved feedback is reviewed by a human on our side. It can become a small, human-approved edit to Bucky's persona, style or knowledge-base prompt. Nothing changes automatically. Make `category` and `expected_reply` required in your form: we rely on both.
5. **`response_id`:** added to `/api/v2/chat` (section 6).

## 3. Authentication and limits

- `Authorization: Bearer {BUCKY_API_KEY}`: the **same key** as `/api/v2/chat` (our `CHAT_API_KEY`).
- `Content-Type: application/json`, UTF-8.
- Body up to **1,000,000 bytes** and **500 transcript messages**.
- Respond time: we store and answer immediately (no model call in the request). Use a 10 s timeout.

## 4. `POST /api/v2/feedback`

Send one approved item.

### Request body

```json
{
  "feedback_id": "fb_6af3a1c2e4b0d1f2a3b4c5d6",
  "submitted_at": "2026-11-15T10:42:11.000Z",
  "approved_at": "2026-11-15T11:05:40.000Z",

  "flagged": {
    "message_id": "6af2c86b7e6f2772e1b5c0f1",
    "response_id": "resp_8f2c4a1b9d3e7f6a5b4c3d2e",
    "text": "I can't find an offer called that in our listings, so I don't want to guess…",
    "sent_at": "2026-11-10T07:31:55.893Z",
    "delivered": false,
    "source": "conversation"
  },

  "feedback": {
    "category": "wrong_information",
    "expected_reply": "The ₹2,500 combo is 54M or 85M bungee + 12 km rafting free + bungee video + transport. Reply BOOK to get the booking details.",
    "note": "This offer is from our Instagram ad. Bucky should know the current ads.",
    "submitted_by": { "id": "6ae38302c3b39d97df23f976", "name": "Annu" },
    "approved_by": { "id": "6ae2…", "name": "Rohan Parmar", "via": "whatsapp" }
  },

  "bucky": {
    "session_id": "wa-6af2c865e5a1796b0f2f1a5a",
    "request_id": "6af2c8657e6f2772e1b5c0bc"
  },

  "conversation": {
    "id": "6af2c8657e6f2772e1b5c0bd",
    "channel": "whatsapp",
    "business_number": "919286136741",
    "started_at": "2026-11-10T07:31:45.000Z",
    "source": {
      "type": "ad",
      "headline": "₹2500 Bungee + FREE 12KM Rafting",
      "ad_text": "Choose between 54M & 85M Bungee and get 12KM River Rafting FREE…",
      "url": "https://www.instagram.com/p/Dd4vt-wghnP/"
    },
    "customer": { "name": "Manish", "phone": "918860270303" },
    "labels": ["Call today"],
    "tags": ["Interested"]
  },

  "transcript": [
    { "id": "6af2c8657e6f2772e1b5c0bc", "at": "2026-11-10T07:31:45.000Z", "role": "customer", "type": "text",
      "text": "What is the ₹2,500 Bungee Combo Offer?" },
    { "id": "6af2c8687e6f2772e1b5c0d9", "at": "2026-11-10T07:31:52.000Z", "role": "team", "sender": "WhatsApp Business app",
      "type": "text", "text": "54M / 85M Bungee – ₹2,500 …" },
    { "id": "6af2c86b7e6f2772e1b5c0f1", "at": "2026-11-10T07:31:55.893Z", "role": "bucky", "type": "text",
      "text": "I can't find an offer called that in our listings…", "delivered": false, "flagged": true }
  ],
  "truncated": false
}
```

### Field reference

Required = we answer 422 without it. Unknown extra fields are accepted and stored, never rejected.

| Field | Required | Type | Notes |
|---|---|---|---|
| `feedback_id` | **yes** | string, 1–200 chars | **The idempotency key.** Must be the same on every retry of the same item, and different for different items. |
| `submitted_at`, `approved_at` | no | ISO 8601 string | |
| `flagged.message_id` | **yes** | string | Your id of the flagged Bucky reply. |
| `flagged.response_id` | no | string | The `response_id` we returned for that reply. Send it whenever you have it. `null` for replies from before this release. |
| `flagged.text` | no | string, ≤ 20,000 | The reply as Bucky wrote it. |
| `flagged.delivered` | no | boolean | `false` if the CRM held the reply back. Still useful. |
| `flagged.source` | no | `conversation` \| `team_cover` \| `follow_up` | Anything else: 422. |
| `feedback.category` | **yes** | enum | `wrong_information`, `wrong_price`, `missed_intent`, `should_have_handed_off`, `should_not_have_handed_off`, `tone`, `formatting`, `too_long`, `other`. Anything else: 422. |
| `feedback.expected_reply` | **yes** | string, 1–10,000 | What the agent says Bucky should have said, in the agent's words. |
| `feedback.note` | no | string, ≤ 10,000 | Extra context. |
| `feedback.submitted_by`, `feedback.approved_by` | no | object | Kept for the audit trail. |
| `bucky.session_id`, `bucky.request_id` | no (send them) | string | Exactly what you sent in the `/api/v2/chat` call that produced the reply. |
| `conversation` | no | object | Free-form. `conversation.source` (the ad) is the most useful part. `customer.name` is dropped and `customer.phone` is masked by us. |
| `transcript` | **yes** | array of objects | The whole chat, **oldest first, frozen at submission time**. At most **500** entries. Each entry: `id`, `at`, `role` (`customer`, `bucky`, `team`, `system`), `type`, `text`, optional `flagged: true`. Media as a placeholder (`"[image]"`) plus caption. No internal notes. |
| `truncated` | no | boolean | `true` if you cut an older part of the chat to fit 500. |

## 5. Responses

| Status | Body | Meaning / what the CRM does |
|---|---|---|
| `200` | `{ "received": true, "ref": "bf_9c1e…" }` | Stored. Mark the item **Delivered** and keep `ref`. |
| `200` | `{ "received": true, "duplicate": true, "ref": "bf_9c1e…" }` | We already had this `feedback_id` (same `ref` as the first time). Treat as success. |
| `401` | `{ "detail": "Unauthorized" }` | Wrong or missing key. **Stop**, show Failed. Do not retry. |
| `422` | `{ "error": "feedback.category: Input should be … ; transcript: Field required" }` | The body is invalid, with the fields that are wrong. We never echo customer text in this message. **Stop**, show Failed with the error. Do not retry. Also returned when the body is over 1 MB or the transcript over 500 messages. |
| `503` | `{ "error": "could not store feedback, please retry" }` (or `{ "detail": "CRM chat API not configured (set CHAT_API_KEY)" }`) | Our side cannot store it right now. **Retry** with your backoff. |
| `409` | — | Reserved. We never return it (a duplicate is a `200`), but keep retrying on it as agreed. |
| other `5xx`, timeout | — | **Retry** with your backoff. |

### Retries and idempotency

- The same `feedback_id` can arrive any number of times; we store it **once**. Retrying is always safe.
- Do **not** change the body between retries of one item (the first one wins).
- A rejected-then-edited feedback goes back for review on your side and should arrive as a **new** `feedback_id` once approved (only the approved version is sent, as you described).

## 6. `response_id` in `/api/v2/chat`

Every non-empty reply from `POST /api/v2/chat` (and the final frame of `/api/v2/chat/stream`) now carries:

```json
{ "session_id": "wa-1-1", "reply": "…", "messages": ["…"], "handoff": false, "handoff_reason": null,
  "session_capped": false, "error": null, "response_id": "resp_8f2c4a1b9d3e7f6a5b4c3d2e" }
```

What the CRM must do:

1. **Store `response_id` on the Bucky message** you save (next to your own message id).
2. When an agent flags that message, send it as `flagged.response_id`.

Notes:
- `response_id` is `null` only when `reply` is empty (an error). Do not send feedback for those.
- A `request_id` replay (your retry of a chat call) returns the **same** `response_id` as the first answer.
- Older clients that ignore unknown JSON fields keep working unchanged.

## 7. Your side: checklist

- [ ] Save `response_id` from every `/api/v2/chat` reply.
- [ ] A stable, unique `feedback_id` per approved item, reused on retries.
- [ ] Send the approved item to `POST /api/v2/feedback` with the Bucky key (section 3).
- [ ] Handle the responses in section 5: 200 = Delivered, 401/422 = Failed (show our `error`), 503/5xx/timeout/409 = retry.
- [ ] Only approved items are sent. Pending, rejected and failed ones never are.
- [ ] Keep `category` and `expected_reply` required in the agent form.
- [ ] Do not send internal agent notes inside `transcript`.

## 8. Not built yet: status callback to the CRM

Your proposal's optional phase 2 (`POST {CRM_URL}/api/v1/webhooks/bucky/feedback` with `applied` / `wont_apply`, signed `X-Bucky-Signature: sha256=<HMAC-SHA256 of the raw body>`) is **not implemented**. If you want it, tell us the URL and the shared secret and we will add it; the status is already tracked on our side. Until then **Delivered** is the last status the CRM can know.

## 9. Try it

```bash
curl -s -X POST "$BUCKY_API_URL/api/v2/feedback" \
  -H "Authorization: Bearer $BUCKY_API_KEY" -H "Content-Type: application/json" \
  -d '{
    "feedback_id": "fb_test_1",
    "flagged": { "message_id": "m2", "text": "wrong price" },
    "feedback": { "category": "wrong_price", "expected_reply": "The price is 2,500." },
    "bucky": { "session_id": "wa-1", "request_id": "r1" },
    "transcript": [ { "id": "m1", "role": "customer", "type": "text", "text": "price?" },
                    { "id": "m2", "role": "bucky", "type": "text", "text": "wrong price", "flagged": true } ]
  }'
# first call:  {"received":true,"ref":"bf_…"}
# same again:  {"received":true,"ref":"bf_…(same)","duplicate":true}
```

## 10. Going live: suggested order

1. **Now, no risk:** start storing `response_id` from `/api/v2/chat` replies (section 6). It is already returned today. Messages sent before you start storing it simply have no `response_id`; send `null` for those.
2. **Test the endpoint** with the curl in section 9, using ids that start with `fb_test_`. There is **no delete endpoint**: test items stay in our database until we clear them by hand, so tell us when you are done testing and we will remove every `fb_test_` item.
3. Check the three outcomes once each: a first send (`200`, `ref`), the same send again (`200`, `duplicate: true`, same `ref`), and a deliberately bad body (`422` with a readable `error`).
4. Switch on sending for real approved items, with your retry rules from section 5.
5. Tell us the date you go live so we watch the first items.

Please keep the `/api/v2/chat` call pattern unchanged: `session_id` and `request_id` must be exactly what you later send in `bucky.session_id` / `bucky.request_id`.

## 11. What happens to an item on our side (for context)

1. Stored with status `received`.
2. A reviewer-controlled process may turn it into a **draft** edit to one of three prompts (the WhatsApp persona, the WhatsApp style, the knowledge base). A draft is never live.
3. A human on our team approves or rejects the draft. Approved drafts go live immediately; the previous prompt is kept for 45 days (last 2 versions) so we can go back.
4. Everything else (code, tools, safety and handoff rules, offers and price messages) is outside what this process can change.

Nothing in this flow changes how Bucky answers customers until a human approves a draft.
