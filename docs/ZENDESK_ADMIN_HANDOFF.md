# Hướng dẫn cho Zendesk Admin: cấp quyền truy cập Zendesk cho hệ thống tích hợp

---

## Bước 1. Chọn tài khoản đứng tên và đăng nhập

Chọn **một** trong hai cách:

|                                   | Cách 1: Tài khoản riêng cho hệ thống (khuyến nghị) | Cách 2: Dùng luôn tài khoản Admin của bạn          |
| --------------------------------- | -------------------------------------------------- | -------------------------------------------------- |
| Ticket do hệ thống tạo hiện tên   | Tài khoản riêng (ví dụ: `Integration Bot`)         | Tên của bạn                                        |
| Khi bạn nghỉ việc / đổi tài khoản | Hệ thống vẫn chạy bình thường                      | Hệ thống **ngừng chạy**, phải tạo lại OAuth client |
| Agent seat                        | Tốn thêm **1 agent seat**                          | Không tốn thêm                                     |
| Thời gian                         | Thêm khoảng 5 phút                                 | Nhanh nhất                                         |

**Yêu cầu chung:** tài khoản được chọn phải có role **Admin**, và **giữ role Admin** trong suốt thời gian hệ thống hoạt động (xem mục **"Lưu ý khi quản lý tài khoản đứng tên về sau"**).

### Nếu chọn Cách 1: tạo tài khoản riêng

1. Vào **Admin Center**: bấm biểu tượng **4 ô vuông** ở góc trên bên phải Zendesk, chọn **Admin Center**.
2. Ở menu bên trái, chọn **People** → **Team** → **Team members** → bấm **Add user**.
3. Điền **Name** và **Email** do bạn tự chọn (email phải là hộp thư bạn mở được để verify). **Role**: **Admin**.
4. Bấm **Save**, mở hộp thư đó và bấm link **verify / đặt password**. Lưu password vào password manager của công ty.
5. Mở một **cửa sổ Incognito** trên trình duyệt (Chrome: `Ctrl + Shift + N`, Mac: `Cmd + Shift + N`), đăng nhập Zendesk bằng tài khoản vừa tạo.
6. Kiểm tra: bấm vào avatar ở góc trên bên phải, phải thấy đúng tên tài khoản vừa tạo.

> **Rất quan trọng:** Bước 2 phải làm trong cửa sổ Incognito, **đang đăng nhập bằng tài khoản riêng này**, không phải tài khoản của bạn.

### Nếu chọn Cách 2: dùng tài khoản Admin của bạn

Không cần làm gì thêm. Đăng nhập Zendesk bằng tài khoản của bạn như bình thường và sang Bước 2.

✅ Xong Bước 1 khi: đang đăng nhập Zendesk bằng **đúng tài khoản đã chọn**, và tài khoản đó là **Admin**.

---

## Bước 2. Tạo OAuth client

> Làm khi đang đăng nhập bằng **tài khoản đã chọn ở Bước 1**.

**OAuth client** giống như "chìa khóa" để hệ thống của dev đăng nhập vào Zendesk. **Scopes** quyết định chìa khóa này **được làm gì**.

1. Vào **Admin Center** → **Apps and integrations** → **APIs** → **OAuth clients**.
2. Bấm **Add OAuth client**.
3. Điền:

   | Ô             | Điền / Chọn                                                |
   | ------------- | ---------------------------------------------------------- |
   | Name          | `Tidesquare Integration`                                   |
   | Description   | `Hệ thống nội bộ gửi và đồng bộ ticket`                    |
   | Company       | `Tidesquare`                                               |
   | Identifier    | `tidesquare_integration`                                   |
   | Client kind   | **Confidential** (mặc định đang là _Public_, **phải đổi**) |
   | Redirect URLs | **Để trống**                                               |

4. Ở phần **Scopes**, **chỉ tick** các ô sau:

   | Tick ô này                 | Nghĩa là hệ thống được...                                                                                                                              |
   | -------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------ |
   | `read`                     | **Xem** dữ liệu Zendesk: ticket, lịch sử thay đổi (audit), custom field, requester, assignee, group, organization; export toàn bộ ticket về để lưu trữ |
   | `tickets:write`            | Tạo ticket, đổi status, public reply, internal note, gán assignee/group, gắn tag, đổi priority, merge ticket                                           |
   | `ticket_attachments:write` | Thêm attachment vào ticket                                                                                                                             |
   | `users:write`              | Tạo requester mới khi ticket được gửi từ người chưa có trong Zendesk                                                                                   |

   **Không tick** các ô khác (đặc biệt **không tick `write`** và **`impersonate`**).
   Nhờ vậy, hệ thống **xem** được dữ liệu, nhưng **chỉ sửa** được ticket, attachment và requester. Hệ thống **không** sửa được trigger, cài đặt tài khoản hay các phần khác của Zendesk.

   > **Vì sao cần `read` thay vì `tickets:read`?** Zendesk chỉ cho phép export toàn bộ ticket, xem lịch sử thay đổi và đọc custom field khi có quyền `read`. Quyền `tickets:read` không đủ cho các việc này.

5. Bấm **Save**.
6. Sau khi lưu, ô **Secret** sẽ hiện ra. Bấm **Copy** và lưu trữ lại để gửi cho dev.
   ⚠️ Secret **chỉ hiện đầy đủ một lần**. Nếu lỡ mất: mở lại OAuth client này → **Regenerate** → copy secret mới.

✅ Xong Bước 2 khi: đã lưu được **Identifier** (`tidesquare_integration`) và **Secret**.

Nếu dùng Cách 1: từ đây có thể **đóng cửa sổ Incognito** và quay lại tài khoản Admin của bạn.

---

## Bước 3. Tạo webhook (để Zendesk báo cho hệ thống khi ticket thay đổi)

1. Vào **Admin Center** → **Apps and integrations** → **Webhooks** → **Webhooks**.
2. Bấm **Create webhook**.
3. Chọn **Trigger or automation** (**không** chọn _Zendesk events_) → bấm **Next**.
4. Điền:
   | Ô | Điền / Chọn |
   | -------------- | -------------------------------- |
   | Name | `Tidesquare Integration` |
   | Endpoint URL | `https://api.zendesk.tidesquare.vn/webhooks/zendesk` |
   | Request method | **POST** |
   | Request format | **JSON** |
   | Authentication | **None** |
5. Bấm **Create webhook**.
6. Mở webhook vừa tạo → bấm **Reveal secret** → copy **Signing secret** và lưu vào password manager.

✅ Xong Bước 3 khi: đã lưu được **Signing secret**.

---

## Bước 4. Tạo trigger (khi ticket đổi status thì gọi webhook)

1. Vào **Admin Center** → **Objects and rules** → **Business rules** → **Triggers**.
2. Bấm **Create trigger**.
3. Điền:
   - **Trigger name:** `Tidesquare Integration - status changed`
   - **Trigger category:** chọn **Notifications** (chỉ để phân loại, chọn mục nào cũng được).

4. Phần **Conditions**, mục **Meet ALL of the following conditions**, bấm **Add condition** và chọn:

   | Ô        | Chọn                           |
   | -------- | ------------------------------ |
   | Category | **Ticket** → **Ticket status** |
   | Operator | **Changed**                    |
   | Value    | Để trống                       |

5. Phần **Actions**, bấm **Add action** và chọn: **Notify by** → **Active webhook** → `Tidesquare Integration`.
6. Ô **JSON body** hiện ra → **xóa hết nội dung có sẵn**, rồi copy **nguyên văn** đoạn dưới đây và dán vào (không sửa gì):

   ```json
   {
     "invocation_id": "{{ticket.id}}-{{ticket.status}}-{{ticket.updated_at_with_timestamp}}",
     "ticket_id": {{ticket.id}},
     "action": "{{ticket.status}}"
   }
   ```

7. Bấm **Create**.

✅ Xong Bước 4 khi: thấy trigger `Tidesquare Integration - status changed` trong danh sách và đang **Active**.

> Ghi chú: tài khoản Zendesk nên để ngôn ngữ **English**. Ticket đã **Closed** thì Zendesk không chạy trigger nữa, điều này bình thường.

---

## Bước 5. Cài app trên sidebar của ticket (tùy chọn, chỉ làm khi dev gửi file `.zip`)

Chỉ cần làm **một lần**. Sau này dev sửa giao diện app thì không cần Admin làm lại.

1. Vào **Admin Center** → **Apps and integrations** → **Zendesk Support apps**.
2. Bấm **Upload private app**.
3. App name: `Tidesquare Integration` → chọn file `.zip` dev gửi → bấm **Upload**.
4. Màn hình **Installation** hiện ra, điền:
   | Ô | Chọn |
   | -------------------------- | ---------------------------------------------------------- |
   | Title | `Tidesquare Integration` |
   | Enable role restrictions? | **Bỏ tick** (để mọi agent đều thấy app) |
   | Enable group restrictions? | **Bỏ tick** |
5. Bấm **Install**.

✅ Xong Bước 5 khi: mở một ticket bất kỳ → bấm biểu tượng **Apps** ở sidebar bên phải → thấy app `Tidesquare Integration`.

---

## Bước 6. Gửi thông tin lại cho dev

Gửi qua **password manager** hoặc kênh bảo mật. **Không** dán vào Slack, email hay chat thông thường.

| Thông tin                                                                  | Lấy ở đâu            |
| -------------------------------------------------------------------------- | -------------------- |
| Subdomain Zendesk (ví dụ: link là `tourvis.zendesk.com` thì gửi `tourvis`) | URL trên trình duyệt |
| Identifier: `tidesquare_integration`                                       | Bước 2               |
| Secret của OAuth client                                                    | Bước 2               |
| Signing secret của webhook                                                 | Bước 3               |

**Không cần** gửi email hay password của tài khoản đứng tên.

Dev sẽ báo lại khi kết nối thành công.

---

## Kiểm tra sau khi xong

1. Dev tạo thử một ticket từ hệ thống → ticket hiện trên Zendesk, người tạo là **tài khoản đã chọn ở Bước 1**.
2. Đổi ticket đó sang **Solved**.
3. Vào **Admin Center** → **Apps and integrations** → **Webhooks** → mở `Tidesquare Integration` → tab **Activity**:
   - Thấy dòng **200**: mọi thứ hoạt động tốt.
   - Thấy dòng **401**: signing secret gửi cho dev bị sai, copy lại ở Bước 3 và gửi lại.
   - Thấy lỗi khác: chụp màn hình gửi dev.

---

## Lưu ý khi quản lý tài khoản đứng tên về sau

Hệ thống có **quyền giống tài khoản đứng tên** (trong giới hạn Scopes). Vì vậy:

- **Không khóa / xóa / suspend** tài khoản này khi hệ thống còn dùng, nếu không hệ thống sẽ ngừng chạy.
- **Giữ role Admin** cho tài khoản này. Zendesk **chỉ cho role Admin** export toàn bộ ticket. Nếu hạ xuống Agent, hệ thống sẽ **không lấy được toàn bộ ticket** về nữa.
- Nếu buộc phải đổi role, hãy **báo dev trước**. Khi đó tài khoản vẫn cần xem được **tất cả ticket** (Ticket access: **All tickets in this account**) và có quyền tạo / sửa / merge ticket.
- **Muốn đổi sang tài khoản khác:** đăng nhập tài khoản mới → làm lại **Bước 2** (tạo OAuth client mới) → gửi Identifier và Secret mới cho dev → đợi dev xác nhận chạy OK → xóa OAuth client cũ.

---

## Gặp vấn đề?

| Tình huống                                                    | Cách xử lý                                                                                                                                                          |
| ------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Bước 1 (Cách 1): Zendesk báo email **đã tồn tại**             | Email này đang được dùng trong Zendesk (có thể là email khách hàng hoặc email nhận ticket). Chọn một email khác.                                                    |
| Bước 1 (Cách 1): không nhận được email verify                 | Kiểm tra thư mục Spam. Hoặc vào Team members → mở tài khoản đó → gửi lại invite.                                                                                    |
| Bước 2: không thấy phần **Scopes**                            | Bỏ qua phần đó, lưu bình thường, và **báo dev** để dev giới hạn scope ở phía hệ thống.                                                                              |
| Bước 2: không thấy **Client kind**                            | Bỏ qua, lưu bình thường và báo dev.                                                                                                                                 |
| Lỡ tạo OAuth client bằng **tài khoản không đúng ý**           | Xóa OAuth client đó, đăng nhập đúng tài khoản rồi làm lại Bước 2.                                                                                                   |
| Đổi role / quyền của tài khoản đứng tên xong thì hệ thống lỗi | **Báo dev** kèm thời điểm thay đổi. Nếu cần chạy gấp, trả lại role cũ trong lúc dev kiểm tra.                                                                       |
| Dev báo **Webhook URL thay đổi**                              | Mở webhook `Tidesquare Integration` → sửa **Endpoint URL** → **Update**. Không cần tạo lại trigger. Nếu Zendesk báo secret thay đổi thì gửi lại secret mới cho dev. |
| Ticket đổi status nhưng hệ thống không nhận được              | Báo dev.                                                                                                                                                            |

> Lưu ý thêm: Zendesk đang deprecate API token (không cho tạo mới từ 27/10/2026), nên tài liệu này dùng OAuth client. **Không cần** tạo API token.
