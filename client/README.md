# client/ - UI sidebar (Vite + React + TypeScript, Cloudflare Pages)

Static site hiển thị trong sidebar ticket Zendesk: thông tin ticket, nguồn Slack (`ticket.externalId`), reaction theo status và nút "Mở Tidesquare". Manifest nằm ở [`../zaf/`](../zaf).

## Chạy local

```bash
cd client
npm install
npm run dev      # mở ngoài Zendesk sẽ hiện "Đang chạy ngoài Zendesk" + nút Tidesquare
npm test         # Vitest
npm run build    # output: dist/ (có dist/_headers)
```

## Deploy Cloudflare Pages

- Dashboard: Framework preset `React (Vite)` (hoặc None); Root directory `client`; Build command `npm run build`; Build output directory `dist`. Node version lấy từ `.node-version`.
- CLI: `npm run deploy` (= build + `wrangler pages deploy dist --project-name slack-bridge-zaf`).

`public/_headers` chỉ cho phép nhúng iframe từ `https://*.zendesk.com` (không dùng `X-Frame-Options`); `/assets/*` cache immutable vì file có hash.

## Thử trong Zendesk trước khi admin cài

App remote-hosted nên `zaf/manifest.json` phải trỏ tới URL đang chạy (hiện là `https://zendesk.kei.info.vn/`, hoặc tunnel). Chạy `npx @zendesk/zcli apps:server zaf`, mở ticket và thêm `?zcli_apps=true` vào URL.
