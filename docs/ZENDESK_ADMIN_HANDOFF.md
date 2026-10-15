# Bàn giao cho Zendesk Admin

## 1. Mục đích

Hệ thống tự tạo ticket Zendesk từ tin nhắn Slack, và khi ticket đổi trạng thái thì tự thả emoji lên tin nhắn Slack gốc.
Cần Admin vì tạo tài khoản dịch vụ, OAuth client, webhook, trigger và cài app đều là quyền admin. Không cần viết code.
Lưu ý: Zendesk đang bỏ dần API token (chặn tạo mới từ 27/10/2026), nên dùng OAuth client thay thế. Menu có thể khác chút tùy giao diện.

## 2. Thông tin dev cung cấp cho Admin

| Mục | Giá trị |
| --- | ------- |
| Webhook URL | `https://4a0a-27-72-105-193.ngrok-free.app/webhooks/zendesk` |
| Gói app ZAF (.zip, tùy chọn) | `slack-bridge-zaf.zip` (đính kèm) |
| URL giao diện sidebar app tải (chỉ để biết) | `https://zendesk.kei.info.vn` |

> Backend hiện chạy trên URL ngrok thử nghiệm. Nếu URL đổi, dev sẽ gửi URL mới và Admin chỉ cần sửa **Endpoint URL** của webhook (không cần tạo lại trigger; signing secret thường giữ nguyên khi sửa webhook đã có, nhưng nếu Zendesk báo khác thì lấy lại secret và gửi cho dev).

## 3. Việc Admin cần làm

1. **Tạo service user (tài khoản dịch vụ)**
   - Admin Center > People > Team > Team members > **Add user**.
   - Tên: `Slack Bridge (Integration)`; email riêng mà team truy cập được (cần xác minh email); Role: **Agent** (hoặc custom role).
   - Quyền cần: xem, tạo, sửa ticket (thêm comment, tag) trong các group/brand liên quan. Cấp quyền tối thiểu, không cần admin.
   - Có thể chiếm một seat agent (chưa xác nhận).
2. **Tạo OAuth client (client credentials)**
   - Admin Center > Apps and integrations > APIs > **OAuth clients** > **Add OAuth client**.
   - **Quan trọng:** đăng nhập bằng **service user ở bước 1** rồi mới tạo. Mọi ticket/thao tác của hệ thống sẽ đứng tên người tạo OAuth client; nếu tạo bằng tài khoản cá nhân thì ticket sẽ hiện tên người đó, và khi tài khoản đó bị khóa/nghỉ việc thì hệ thống ngừng chạy. (Nếu service user chưa có quyền tạo OAuth client: tạm cấp quyền admin hoặc "Manage APIs", tạo xong thì hạ quyền.)
   - Điền form:

     | Ô | Giá trị |
     | --- | --- |
     | Name | `Slack Bridge` |
     | Company | `Tidesquare` |
     | Identifier | `slack_bridge` |
     | Client kind | **Confidential** (mặc định là Public, phải đổi) |
     | Redirect URLs | để trống |
     | Scopes | chọn `tickets:read` và `tickets:write` |

   - Bấm **Save**, sau đó ô **Secret** mới hiện → bấm **Copy** (chỉ hiện đầy đủ một lần; mất thì vào Edit để regenerate và gửi lại cho dev).
3. **Custom ticket field**: không cần, bỏ qua.
4. **Tạo Webhook**
   - Admin Center > Apps and integrations > Webhooks > Webhooks > **Create webhook**.
   - Chọn **Trigger or automation** (KHÔNG chọn Zendesk events), bấm Next.
   - Name: `Slack bridge`; Endpoint URL: URL ở mục 2; Request method: **POST**; Request format: **JSON**; Authentication: **None**.
   - Bấm Create webhook, mở webhook vừa tạo, bấm **Reveal secret**, copy **signing secret** (Zendesk tự sinh, không tự đặt được).
5. **Tạo Trigger (chỉ cần 1 trigger)**
   - Admin Center > Objects and rules > Business rules > Triggers > **Create trigger**.
   - Trigger name: `Slack bridge - status changed`.
   - Trigger category: chọn **Notifications** (category chỉ để gom nhóm, không ảnh hưởng hoạt động; muốn tách riêng thì bấm **Create category** và đặt tên `Integrations`).
   - Conditions, phần **Meet ALL of the following conditions**:

     | Ô | Chọn |
     | - | ---- |
     | Category | Ticket > Ticket status |
     | Operator | Changed |
     | Value | (để trống) |

   - Actions: chọn **Notify by > Active webhook** > `Slack bridge`, rồi dán JSON body này:

     ```json
     {
       "invocation_id": "{{ticket.id}}-{{ticket.status}}-{{ticket.updated_at_with_timestamp}}",
       "ticket_id": {{ticket.id}},
       "action": "{{ticket.status}}"
     }
     ```
   - Bấm **Create** / Save. Không cần sửa gì trong JSON.

   Emoji hệ thống sẽ thả (chỉ để biết):

   | Trạng thái | Emoji |
   | ---------- | ----- |
   | New, Open | 👀 `eyes` |
   | Pending, On-hold | ⏳ `hourglass_flowing_sand` |
   | Solved, Closed | ✅ `white_check_mark` |

   Lưu ý:
   - Tài khoản nên để ngôn ngữ **English** (trạng thái hiển thị là `Open`, `Solved`...). Nếu tài khoản dùng ngôn ngữ khác hoặc có custom ticket status mà emoji không hiện, báo dev và dùng cách ở **Phụ lục**.
   - Zendesk không chạy trigger cho ticket đã **Closed**, nên trạng thái này không thả emoji. Bình thường, không cần xử lý.
6. **Cài app ZAF** (nếu dev gửi zip)
   - Zip được đóng gói từ thư mục `zaf/` (chỉ có manifest; UI host trên Cloudflare Pages). Chỉ upload **một lần**; sửa UI sau này không cần admin.
   - Admin Center > Apps and integrations > Zendesk Support apps > **Upload private app** → App name: `Slack Bridge`, chọn file zip → **Upload**.
   - Màn hình **Installation** hiện ra, điền:

     | Ô | Chọn |
     | --- | --- |
     | Title | `Slack Bridge` (tên hiện trên sidebar ticket) |
     | Enable role restrictions? | **Bỏ tick** để mọi agent đều thấy app. Nếu muốn giới hạn thì giữ tick và **phải chọn ít nhất 1 role** (ví dụ Agent, Admin), không để tick mà ô trống |
     | Enable group restrictions? | Để trống (không tick), trừ khi chỉ muốn một vài group dùng |

   - Bấm **Install**. Kiểm tra: mở một ticket bất kỳ → bấm biểu tượng **Apps** ở thanh bên phải → thấy app `Slack Bridge` với nút "Mở Tidesquare".

## 4. Admin gửi lại cho dev

Gửi qua kênh bảo mật (password manager, không dán vào chat thường).

| Thông tin | Biến môi trường |
| --------- | --------------- |
| Subdomain (phần `acme` của `acme.zendesk.com`) | `ZENDESK_SUBDOMAIN` |
| Unique identifier của OAuth client (`slack_bridge`) | `ZENDESK_OAUTH_CLIENT_ID` |
| Secret của OAuth client | `ZENDESK_OAUTH_CLIENT_SECRET` |
| Email service user (để biết, không cần cấu hình) | - |
| Signing secret của webhook | `ZENDESK_WEBHOOK_SECRET` |

## 5. Kiểm tra

Đổi một ticket (tạo từ Slack) sang Solved, kiểm tra tin nhắn Slack gốc có emoji dấu tích. Xem webhook > tab **Activity** để thấy các lần gửi (HTTP 200 là tốt, 401 là sai secret).

## Phụ lục: dự phòng, 1 trigger cho mỗi trạng thái

Chỉ dùng khi cách 1 trigger không thả được emoji. Tạo 4 trigger, mỗi trigger có điều kiện `Ticket > Status | Changed to | <trạng thái>` và dùng JSON ở bước 5 nhưng thay `{{ticket.status}}` trong dòng `"action"` bằng giá trị cố định (không dùng `{{ticket.status}}` ở dòng `action`):

| Tên trigger | Changed to | `"action"` |
| ----------- | ---------- | ---------- |
| Slack bridge - open | Open | `"open"` |
| Slack bridge - pending | Pending | `"pending"` |
| Slack bridge - hold | On-hold | `"hold"` |
| Slack bridge - solved | Solved | `"solved"` |
