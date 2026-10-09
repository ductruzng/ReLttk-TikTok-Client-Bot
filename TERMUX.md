# Samsung S21 Ultra: chuẩn bị chạy trong Termux

Bot dùng API nội bộ TikTok không chính thức. **Chưa xác nhận tin gửi bằng bot có
được tính vào chuỗi TikTok hay không.** Chưa kiểm thử trên điện thoại, đăng nhập
thật hoặc gửi thật; hướng dẫn này không bảo đảm giao thức hiện tại còn hoạt động.

## 1. Môi trường và dependency

Dùng bản mã nguồn hiện có, đặt trong thư mục riêng của Termux, ví dụ
`$HOME/ReLttk-TikTok-Client-Bot`. Không lưu phiên trong `/sdcard` hoặc Download:
bộ nhớ chia sẻ không cung cấp cùng quyền POSIX và khả năng thực thi như thư mục
riêng. Xem [môi trường thực thi Termux](https://github.com/termux/termux-packages/wiki/Termux-execution-environment).

Mã yêu cầu Python 3.10 trở lên. Các lệnh dưới đây là hướng dẫn để anh tự thực hiện
trên điện thoại; chưa được thực thi trong đợt chỉnh sửa này:

```sh
pkg update
pkg install python python-pip clang make pkg-config liblz4 ca-certificates
cd "$HOME/ReLttk-TikTok-Client-Bot"
python -m venv .venv
source .venv/bin/activate
python -m pip install --no-binary=lz4 -r requirements.txt
python -m pip check
```

Không dùng `pip install --upgrade pip` để thay pip do Termux quản lý. `venv` chỉ
quản lý dependency, **không phải sandbox bảo mật**; mã vẫn có quyền truy cập các
file và mạng của ứng dụng Termux.

- `websockets`: giới hạn nhánh 15.x; dùng kết nối TLS có kiểm tra chứng chỉ/hostname.
- `lz4`: có phần mở rộng C. Android dùng Bionic; wheel manylinux (glibc) hoặc
  musllinux (musl) không thay thế được bản Android. Lệnh trên yêu cầu biên dịch
  nguồn; thư viện có thể dùng LZ4 hệ thống hoặc bản kèm theo. Xem
  [hướng dẫn python-lz4](https://python-lz4.readthedocs.io/en/stable/install.html).
- `qrcode`: hiển thị QR trên terminal, không cần Pillow cho đường chạy này.
- `tzdata`: cung cấp múi giờ `Asia/Ho_Chi_Minh`.
- Dependency không sử dụng `stealth-requests` đã bỏ. `pycryptodome` nằm riêng trong
  `requirements-windows.txt`; không cần cài cho Termux. Nhập cookie trình duyệt
  Windows không được hỗ trợ trên Android và không được CLI này tự gọi.

Đã kiểm tra cục bộ trên Windows/Python 3.10.11 với websockets 15.0.1, lz4 4.4.5,
qrcode 8.2, tzdata 2026.5. Đây **không phải** kết quả kiểm thử Android ARM64.

## 2. Cấu hình và kiểm tra ngoại tuyến

```sh
cp streak.json streak.local.json
python -B main.py --config streak.local.json
```

Sửa `streak.local.json` (đã được Git bỏ qua):

```json
{
  "session": "ten_phien",
  "message": "Chao anh!",
  "targets": [
    {"conv_id": "0:1:111:222", "conv_short_id": 8888, "conv_type": 1}
  ]
}
```

Các ID trên chỉ là ví dụ giả. Cần thay bằng đúng dữ liệu hộp thư của tài khoản;
`conv_type` là 1 cho tin riêng, 2 cho nhóm. Danh sách chỉ gồm các hội thoại anh
chọn; không có gửi hàng loạt theo toàn bộ inbox. Mẫu gốc để trống phiên và targets,
vì vậy chạy nguyên mẫu sẽ báo chưa cấu hình và trả mã thoát 1.

Dry-run không đọc cookie, không dùng mạng, không mở ledger, không nạp plugin và
không ghi nhận đã gửi. Hạn ngạch chỉ được kiểm tra ở luồng gửi có xác thực.

```sh
python -B -m unittest discover -s tests -v
```

Kiểm tra dùng phiên giả trong thư mục tạm và WebSocket giả. Không có đăng nhập
TikTok, gửi tin thật hoặc lịch tự động trong bộ kiểm tra.

## 3. Các lệnh mạng dành cho bước kiểm thử thủ công sau này

**Không coi cấu hình giao thức là đã xác minh hoạt động.** Token mẫu cũ nhúng trong
mã đã bị loại bỏ. Các tham số device, SDK và chữ ký kế thừa từ repo vẫn cần kiểm
tra với phiên hiện tại; không ghi token thật vào file được Git theo dõi. Nếu máy
chủ từ chối hoặc không có echo phù hợp, bot không ghi thành công.

Các lệnh sau có kết nối TikTok, chỉ dùng khi anh chủ động thực hiện bước đó:

```sh
python main.py login
python main.py list-conversations --session ten_phien
python main.py --config streak.local.json --send
```

Đăng nhập cần terminal tương tác để hiển thị QR; không in URL QR chứa token để
làm phương án dự phòng. Cách quét QR trên cùng một điện thoại chưa được kiểm thử.
Lệnh liệt kê bắt buộc chọn phiên rõ ràng. Trước khi gửi, bot đối chiếu ID hội thoại,
short ID và loại hội thoại với inbox của phiên đó; không tìm thấy thì dừng.

Mỗi lần chạy gửi một lượt rồi kết thúc. Không cài cron, Tasker, Termux:Boot hay
lịch tự động. Android có thể dừng tiến trình nền; xem
[tài liệu Termux](https://github.com/termux/termux-app#installation).

## 4. Chống gửi trùng và xử lý trạng thái

Ledger `state/streak_ledger.db` giữ một lượt cho mỗi UID/hội thoại/ngày theo
`Asia/Ho_Chi_Minh`. Lượt `pending` được ghi bền vững trước khi gửi. Chỉ echo từ máy
chủ khớp UUID, hội thoại, UID người gửi, nội dung và server message ID dương mới
chuyển sang `confirmed`. Điều này không chứng minh người nhận đã đọc tin hoặc
TikTok đã tính chuỗi.

- `failed_unknown`: có thể máy chủ đã nhận; không tự thử lại trong ngày.
- `pending` còn sót sau sự cố: chặn cả các ngày tiếp theo để tránh gửi trùng.
  Kiểm tra hội thoại thật và ledger thủ công trước khi xử lý; không xóa database
  chỉ để bỏ qua giới hạn. Không có lệnh tự động giải phóng pending.
- Nếu phản hồi/lỗi vượt qua nửa đêm, ngày mới cũng được chặn thận trọng.

```sh
python main.py status
```

Giới hạn chỉ có hiệu lực khi giữ nguyên một ledger trên một thiết bị. Nó không
bao gồm tin gửi thủ công trên ứng dụng, thiết bị khác, state bị xóa hay đồng hồ
sai. Giữ giờ/ngày của điện thoại chính xác.

## 5. Phiên và dịch vụ kết nối

Phiên là JSON **không mã hóa** trong `sesion/`: thư mục 0700, file 0600 trên POSIX.
Quyền này không bảo vệ khỏi mã khác chạy cùng quyền Termux. Lỗi siết quyền phải
dừng thao tác. Windows không có cùng bảo đảm từ `chmod`; cần ACL của thư mục riêng.
Không đưa phiên, cookie, ledger, log hoặc cấu hình cá nhân lên Git hay bộ nhớ chung.

| Dịch vụ | Mục đích |
| --- | --- |
| `www.tiktok.com`, `web-sg.tiktok.com` | QR/Passport, thông tin tài khoản, API web |
| `im-api-sg.tiktok.com` | Inbox và dữ liệu hội thoại |
| `im-ws-va.tiktok.com` | WebSocket nhắn tin |
| CDN TikTok | Mã hỗ trợ media cũ; không dùng cho tin văn bản theo ngày |
| `linkmail.wtf` | Dịch vụ upload của plugin mẫu trước đây; đường tải/upload đã bị vô hiệu hóa |

Signer chạy trong Python cục bộ, không gọi dịch vụ ký bên ngoài. Plugin mẫu và
chấp nhận người lạ mặc định tắt. API không chính thức có thể đổi, từ chối chữ ký
hoặc hạn chế tài khoản; chưa xác minh được tính chuỗi hay tương thích giao thức thật.
