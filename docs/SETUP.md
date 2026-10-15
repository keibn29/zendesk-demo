# Hướng dẫn cài đặt nhanh

Slack message -> API (FastAPI) -> ticket Zendesk. Zendesk đổi status -> webhook -> API -> reaction Slack.

## 1. Yêu cầu

- Python 3.12+, Docker + Docker Compose (nếu chạy container)
- Node.js (`npx @zendesk/zcli`) nếu cài ZAF client
- URL HTTPS công khai (chạy local: `ngrok http 8000`)
- Quyền tạo Slack app và quyền admin Zendesk

## 2. Cấu hình `.env`

```bash
cp api/.env.example api/.env
```

| Biến | Lấy ở đâu |
| ---- | --------- |
| `SLACK_SIGNING_SECRET` | Slack app -> Basic Information -> Signing Secret |
| `SLACK_BOT_TOKEN` | Slack app -> OAuth & Permissions (`xoxb-...`) |
| `SLACK_CHANNEL_ALLOWLIST` | Tùy chọn: danh sách channel ID, cách nhau bằng dấu phẩy (trống = mọi channel bot ở trong) |
| `ZENDESK_SUBDOMAIN` | Chỉ phần `acme` của `acme.zendesk.com` |
| `ZENDESK_WEBHOOK_SECRET` | Signing secret của webhook Zendesk (bước 4) |
| `ZENDESK_OAUTH_TOKEN` | Ưu tiên dùng; hoặc thay bằng `ZENDESK_EMAIL` + `ZENDESK_API_TOKEN` |

Bắt buộc: 3 secret (`SLACK_SIGNING_SECRET`, `SLACK_BOT_TOKEN`, `ZENDESK_WEBHOOK_SECRET`), `ZENDESK_SUBDOMAIN`, và một cách xác thực Zendesk. Thiếu là app không khởi động.
Map status -> emoji đổi qua `REACTION_NEW/OPEN/PENDING/HOLD/SOLVED/CLOSED` (mặc định: new/open = `eyes`, pending/hold = `hourglass_flowing_sand`, solved/closed = `white_check_mark`).

## 3. Tạo Slack App

1. <https://api.slack.com/apps> -> Create New App -> From scratch.
2. OAuth & Permissions -> Bot Token Scopes: `reactions:write`, cùng `channels:history` (channel công khai) và/hoặc `groups:history` (channel riêng tư).
3. Event Subscriptions -> bật Enable Events, Request URL:
   ```
   https://<domain>/webhooks/slack/events
   # hiện tại: https://4a0a-27-72-105-193.ngrok-free.app/webhooks/slack/events
   ```
   (API phải đang chạy với đúng `SLACK_SIGNING_SECRET` để Slack verify được.)
4. Subscribe bot events: `message.channels` và/hoặc `message.groups`.
5. Install / Reinstall to Workspace (bắt buộc sau mỗi lần đổi scope hoặc event), copy Bot Token vào `.env`.
6. Mời bot vào channel: `/invite @TênBot`.

Chỉ tin nhắn người dùng thường (không bot, không subtype, text không rỗng) mới tạo ticket (subject `[Slack] <dòng đầu>`, tag `slack`).

## 4. Cấu hình Zendesk

Không cần custom field. Ticket được tạo với `external_id = slack:{team}:{channel}:{ts}`.

> Nếu bạn không có quyền admin, gửi [ZENDESK_ADMIN_HANDOFF.md](./ZENDESK_ADMIN_HANDOFF.md) cho admin.

1. API token: Admin Center -> Apps and integrations -> APIs -> Zendesk API (hoặc OAuth token) -> điền `.env`.
2. Webhook: Apps and integrations -> Webhooks -> Create webhook -> chọn **Trigger or automation** (khuyến nghị; payload do mình kiểm soát, khớp parser).
   - Endpoint URL: `https://<domain>/webhooks/zendesk`, POST, JSON, Authentication: None (API tự kiểm chữ ký).
   - Signing secret do Zendesk tự sinh: mở webhook -> Reveal secret -> `ZENDESK_WEBHOOK_SECRET`.
3. Trigger (một trigger cho mỗi status open/pending/hold/solved): điều kiện `Ticket | Status | Changed to | <status>`, action Notify by -> Active webhook, JSON body:
   ```json
   {"invocation_id": "{{ticket.id}}-solved-{{ticket.updated_at_with_timestamp}}", "ticket_id": {{ticket.id}}, "action": "solved"}
   ```
   `ticket_id` phải là số nguyên (không có dấu nháy), `invocation_id` bắt buộc (thiếu -> `400`). Dùng status viết cứng thay vì `{{ticket.status}}` vì giá trị này có thể bị dịch.
   Zendesk Events (`zendesk:ticket.status_changed`) chưa được xác nhận khớp parser nên không khuyến nghị.

## 5. Chạy API

Local:
```bash
cd api
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --reload --port 8000
```

Docker (từ thư mục gốc):
```bash
docker compose up --build
```

Health check:
```bash
curl http://localhost:8000/health   # {"status":"ok"}
```

SQLite lưu ở `api/data/` (Docker: volume `bridge-data`). Chỉ test local mới dùng `ENVIRONMENT=development` + `DEV_DISABLE_SIGNATURE_VERIFICATION=true`.

## 6. Cài ZAF app (tùy chọn, sidebar hiển thị nguồn Slack)

App remote-hosted: `zaf/` chỉ chứa manifest (admin upload một lần), UI trong `client/` deploy lên Cloudflare Pages.

```bash
(cd client && npm install && npm run deploy)   # build + deploy UI lên Cloudflare Pages
# manifest đã trỏ tới https://zendesk.kei.info.vn/
npx @zendesk/zcli apps:validate zaf
npx @zendesk/zcli apps:package zaf       # tạo ZIP trong zaf/tmp/
npx @zendesk/zcli apps:server zaf        # thử trong ticket với ?zcli_apps=true
```

Chi tiết: [`zaf/README.md`](../zaf/README.md), [`client/README.md`](../client/README.md). App là private, không có parameters; client chỉ đọc dữ liệu ticket, không gọi API.

## 7. Kiểm tra nhanh

```bash
cd api && .venv/bin/pip install -r requirements-dev.txt && .venv/bin/pytest
(cd client && npm install && npm test)
```

Smoke test end-to-end:
1. `curl /health` trả `ok`.
2. Gửi tin nhắn thường vào channel đã mời bot -> log có `Created Zendesk ticket from Slack message`, ticket xuất hiện trong Zendesk.
3. Đổi ticket sang `open`/`solved` -> tin nhắn Slack nhận reaction `eyes` / `white_check_mark`.
