# Kết quả triển khai và rà soát — 2026-10-09

Đường chạy chính: CLI trên Termux. TUI trên Windows là tiện ích cấu hình tùy
chọn; Playwright chỉ phục vụ bước lấy auth trên Windows, không nằm trong bộ
dependency lõi. Chưa kiểm thử trên S21 Ultra, chưa đăng nhập/gửi/probe TikTok
thật và không bật lịch trong lượt triển khai này.

## Thay đổi đã thực hiện

- `oneshot.py`, `ledger.py`: sửa nhánh skip; bỏ `force`/`ignore_ledger`, không xóa
  lịch sử hoặc pending để thử lại. `ledger.py` trở về logic an toàn đã có ở HEAD,
  nên không còn xuất hiện trong `git diff` dù đã sửa so với đầu lượt.
- `tiktok_tui.py`, `tui_services.py`: lịch theo ngày+giờ Việt Nam, mặc định tắt;
  dùng chung đường gửi thủ công/lịch và báo riêng confirmed/skipped/failed.
  Validation luôn được gọi, không nuốt ImportError. Nút gửi nói rõ dùng kế
  hoạch đã lưu, không dùng lựa chọn chưa lưu trên màn hình.
- `main.py`, `oneshot.py`, `core/api.py`: bỏ traceback, frame thô và thông báo
  exception chứa nội dung không kiểm soát; chẩn đoán WS chỉ in metadata/status.
- `qrlogin.py`, `client.py`, `ws_auth_capture.py`: dùng lại writer phiên cho auth
  atomic/private, từ chối symlink và lỗi chmod. Browser context mới không lưu
  profile; candidate chỉ nằm trong bộ nhớ. Probe thành công mới lưu auth theo
  phiên ở `sesion/ws-auth/<session>.json`, có fingerprint cookie để chặn ghép
  nhầm hoặc dùng auth cũ sau khi đăng nhập lại.
- `requirements-tui.txt`, `requirements-auth-windows.txt`: dependency tùy chọn
  riêng; không thêm Textual/Playwright vào `requirements.txt` của CLI.
- `README.md`, `TERMUX.md`: các bước Windows → Termux, chuyển ledger khi đổi
  máy, giới hạn scheduler và hướng dẫn `capture-ws-auth` cho phiên đã có.
- Tests: cập nhật test chẩn đoán không in response body/header, mock API inbox;
  thêm `tests/test_safety_fixes.py` cho các ràng buộc mới và TUI smoke test.

Đính chính review trước: `main.validate_config` có tồn tại. Việc TUI import tên
này không phải lỗi; phần cải thiện là bỏ fallback cho phép bỏ qua validation.

## Kiểm tra

- 81 tests pass trên Windows/Python 3.10.11, gồm TUI headless smoke test.
- Runner reviewer ở `work/review_offline.py` chặn DNS/kết nối ngoài và việc mở
  session, auth, plan cá nhân, UID cache, browser profile, ledger thật. Cho phép
  loopback nội bộ mà asyncio Windows cần. Test dùng file tạm và mock.
- `pip check`: pass; Textual 8.2.8, Playwright 1.63.0 đã có trong môi trường.
- Parse AST của 32 file Python: pass; `git diff --check` với `cr-at-eol`: pass.
- `python -B -S main.py --dry-run`: chạy được không cần site-packages; mẫu chưa
  cấu hình trả mã 1 đúng dự kiến. Các test dry-run hợp lệ vẫn pass.
- Không có kiểm thử tương thích giao thức thật, browser capture thật hoặc
  Android ARM64. Không commit/push và không thay dữ liệu runtime cá nhân.

## Ponytail review — phần thay đổi

Đã rà diff và các file nguồn mới chưa tracked. Các dòng dưới đây chỉ là đề xuất,
chưa áp dụng; số dòng là ước tính trên snapshot hiện tại.

- `tiktok_tui.py:L14`: delete: import `re` và `Path` không dùng. Không cần thay thế. (~2 dòng)
- `ws_auth_capture.py:L118`: shrink: biến đếm `elapsed` và nhánh return thừa. Dùng `for _ in range(CAPTURE_MS // 1000)` và `return found_pair`. (~4 dòng)

net: -6 lines possible.

## Ponytail audit — toàn repo

Đã quét 32 file Python thuộc root/core/plugins/signers/tests, đối chiếu caller
và dependency; bỏ qua dữ liệu riêng, venv, state và work. Xếp theo phần có thể
cắt lớn nhất. Những mục bỏ API/diagnostic legacy chỉ phù hợp khi chốt sản phẩm
là bot streak, không tiếp tục hỗ trợ toolkit đa năng upstream; chưa xóa gì.

- delete: công cụ chẩn đoán browser riêng 273 dòng, nếu không cần các bộ đếm/header chi tiết của nó. Luồng chính đã có `capture-ws-auth` và `ws-probe`. [`test_ws_capture.py`](test_ws_capture.py)
- delete: bộ trích cookie trình duyệt Windows không có caller trong luồng CLI/TUI và dependency riêng `pycryptodome`. Dùng QR session + capture đã có; bỏ module và requirements riêng nếu ngừng API legacy này. (~211 dòng, 1 dependency) [`browsercookies.py`](browsercookies.py), [`requirements-windows.txt`](requirements-windows.txt)
- delete: năm plugin mẫu không dùng cho tin cố định theo ngày: info/menu/ping/react/stickerdl. Không cần thay thế; giữ stub videodl và các test bảo vệ việc tắt plugin. (~112 dòng) [`plugins`](plugins)
- delete: `core.api.shorten_url` không có caller nội bộ; bỏ cả export legacy nếu không phục vụ người dùng toolkit bên ngoài. QR login có đường rút URL riêng đang được dùng, không xóa đường đó. (~38 dòng) [`core/api.py:717`](core/api.py)
- shrink: bốn helper mã hóa protobuf lặp lại trong API. Import alias của `core.proto.f_varint/f_bytes/f_str`, giữ validation và test wire format. (~22 dòng) [`core/api.py:377`](core/api.py)

net: -656 lines, -1 deps possible.

Đây là phần có thể cắt, không phải số đã xóa hay cam kết tương đương toàn bộ API
legacy. Không coi kiểm tra an toàn, ledger, xác nhận echo hoặc test hồi quy là
code thừa. Các số review/audit là hai phạm vi riêng, không cộng thành kết quả
đã triển khai.

## Giới hạn vận hành

Auth cũ `ws_auth.local.json` không được tự nhập vào phiên mới. Cần capture lại
trên Windows rồi chuyển cặp phiên/auth đúng đường dẫn; dữ liệu cũ không bị xóa.
Scheduler TUI chỉ chạy khi ứng dụng mở đúng phút, không bù lịch đã lỡ. Ledger
chỉ bảo vệ một thiết bị/state: không chạy đồng thời Windows và Termux với hai
ledger riêng. POSIX 0600/0700 không mã hóa hoặc cách ly mã cùng UID; Windows cần
ACL thư mục riêng. `venv` không phải sandbox bảo mật.

Bot dùng API nội bộ TikTok không chính thức; **chưa xác nhận tin qua bot được
tính vào chuỗi**. Handshake hoặc test offline pass không chứng minh gửi DM thật
thành công.
