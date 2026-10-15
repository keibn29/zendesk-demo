# Bàn giao cho Zendesk Admin

## 1. Mục đích

Hệ thống tự tạo ticket Zendesk từ tin nhắn Slack, và khi ticket đổi trạng thái thì tự thả emoji lên tin nhắn Slack gốc.
Cần Admin vì tạo API token, webhook, trigger và cài app đều là quyền admin. Không cần viết code.

## 2. Thông tin dev cung cấp cho Admin

| Mục | Giá trị |
| --- | ------- |
| Webhook URL | `https://4a0a-27-72-105-193.ngrok-free.app/webhooks/zendesk` |
| Gói app ZAF (.zip, tùy chọn) | `slack-bridge-zaf.zip` (đính kèm) |
| URL giao diện sidebar app tải (chỉ để biết) | `https://zendesk.kei.info.vn` |
| Email agent dùng cho bot | `<dev điền, nên là tài khoản agent riêng>` |

> Backend hiện chạy trên URL ngrok thử nghiệm. Nếu URL đổi, dev sẽ gửi URL mới và Admin chỉ cần sửa **Endpoint URL** của webhook (không cần tạo lại trigger; signing secret thường giữ nguyên khi sửa webhook đã có, nhưng nếu Zendesk báo khác thì lấy lại secret và gửi cho dev).

## 3. Việc Admin cần làm

1. **Tạo API token**
   - Admin Center > Apps and integrations > APIs > Zendesk API.
   - Tab Settings: bật **Token access**, bấm **Add API token**, đặt tên `slack-bridge`, copy token (chỉ hiện một lần).
   - Hệ thống đăng nhập bằng `{email}/token`, nên cho dev biết **email agent** tương ứng (nên tạo agent riêng, ví dụ `slack-bridge@...`, có quyền tạo ticket).
2. **Custom ticket field**: không cần, bỏ qua.
3. **Tạo Webhook**
   - Admin Center > Apps and integrations > Webhooks > Webhooks > **Create webhook**.
   - Chọn **Trigger or automation** (KHÔNG chọn Zendesk events), bấm Next.
   - Name: `Slack bridge`; Endpoint URL: URL ở mục 2; Request method: **POST**; Request format: **JSON**; Authentication: **None**.
   - Bấm Create webhook, mở webhook vừa tạo, bấm **Reveal secret**, copy **signing secret** (Zendesk tự sinh, không tự đặt được).
4. **Tạo Trigger** (mỗi trạng thái một trigger)
   - Admin Center > Objects and rules > Business rules > Triggers > **Create trigger**.
   - Điều kiện (Meet ALL): `Ticket | Status | Changed to | <trạng thái>`.
   - Action: **Notify by > Active webhook** > chọn `Slack bridge`; dán JSON body bên dưới (đổi `STATUS`).

   | Tên trigger | Trạng thái điều kiện | `STATUS` trong JSON | Emoji |
   | ----------- | -------------------- | ------------------- | ----- |
   | Slack bridge - open | Open | `open` | eyes |
   | Slack bridge - pending | Pending | `pending` | hourglass_flowing_sand |
   | Slack bridge - hold | On-hold | `hold` | hourglass_flowing_sand |
   | Slack bridge - solved | Solved | `solved` | white_check_mark |

   ```json
   {
     "invocation_id": "{{ticket.id}}-STATUS-{{ticket.updated_at_with_timestamp}}",
     "ticket_id": {{ticket.id}},
     "action": "STATUS"
   }
   ```
   Lưu ý: `{{ticket.id}}` ở dòng `ticket_id` **không có dấu nháy**. Dùng chữ cố định (`open`, `solved`...) thay vì `{{ticket.status}}` vì giá trị đó có thể bị dịch theo ngôn ngữ tài khoản.
5. **Cài app ZAF** (nếu dev gửi zip)
   - Zip được đóng gói từ thư mục `zaf/` (chỉ có manifest; UI host trên Cloudflare Pages). Admin Center > Apps and integrations > Zendesk Support apps > **Upload private app**, tải zip lên, Install. Chỉ upload **một lần**, không có ô cài đặt nào cần điền; sửa UI sau này không cần admin.

## 4. Admin gửi lại cho dev

Gửi qua kênh bảo mật (password manager, không dán vào chat thường).

| Thông tin | Biến môi trường |
| --------- | --------------- |
| Subdomain (phần `acme` của `acme.zendesk.com`) | `ZENDESK_SUBDOMAIN` |
| Email agent của bot | `ZENDESK_EMAIL` |
| API token | `ZENDESK_API_TOKEN` |
| Signing secret của webhook | `ZENDESK_WEBHOOK_SECRET` |

## 5. Kiểm tra

Đổi một ticket (tạo từ Slack) sang Solved, kiểm tra tin nhắn Slack gốc có emoji dấu tích. Xem webhook > tab **Activity** để thấy các lần gửi (HTTP 200 là tốt, 401 là sai secret).
