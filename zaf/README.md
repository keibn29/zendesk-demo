# zaf/ - gói app Zendesk (chỉ manifest)

Thư mục này chứa **duy nhất** những file admin Zendesk cần upload. UI thật nằm ở [`../client/`](../client) và được host trên Cloudflare Pages.

## Đóng gói

Domain FE hiện tại: `https://zendesk.kei.info.vn/` (đã điền trong `manifest.json`). Đổi domain = sửa manifest và **admin phải upload lại**.

1. Kiểm tra: `npx @zendesk/zcli apps:validate zaf`
2. Đóng gói, chọn một trong hai:
   - `npx @zendesk/zcli apps:package zaf` -> zip nằm trong `zaf/tmp/`
   - `cd zaf && zip -r ../slack-bridge-zaf.zip manifest.json translations assets`
3. Gửi file zip cho admin (Admin Center > Apps and integrations > Zendesk Support apps > Upload private app).

## Khi nào cần admin upload lại?

- **Cần:** đổi `manifest.json` (URL/domain, location, version, parameters), đổi translations hoặc logo.
- **Không cần:** sửa UI (`client/`) - chỉ cần deploy lại Cloudflare Pages.
