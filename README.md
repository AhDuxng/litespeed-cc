# litespeed-cc — Hiệu năng tải file end-to-end qua Starlink với OpenLiteSpeed

Raspberry Pi 4 kết nối Internet qua Starlink và dùng curl tải tệp trực tiếp từ máy chủ
**OpenLiteSpeed** trên AWS. Thí nghiệm khảo sát 3 ngăn xếp giao thức × 2 thuật toán kiểm soát
tắc nghẽn (CCA) phía máy chủ × 2 kích thước tệp, tổng cộng 12 tổ hợp.

| Hạng mục | Giá trị hoặc cách đo |
|---|---|
| Giao thức | HTTP/2 trên TCP+TLS 1.2 · HTTP/2 trên TCP+TLS 1.3 · HTTP/3 trên QUIC (`--http3-only`) |
| Kích thước tệp | 2 MiB (`2MB.bin`) · 50 MiB (`50MB.bin`) |
| CCA phía máy chủ | CUBIC · BBRv1 |
| Cổng | HTTP/2: TCP `443`, dùng chung cho cả hai CCA · HTTP/3: UDP `4461` (CUBIC), UDP `4462` (BBRv1) |
| CCA cho QUIC | mỗi cổng UDP cố định một CCA: `4461` = CUBIC, `4462` = BBRv1 |
| CCA cho TCP | `net.ipv4.tcp_congestion_control` toàn hệ thống: x4:30 → BBRv1, x9:30 → CUBIC |
| Lịch máy khách | phút x0, x10, … gửi yêu cầu với CUBIC · phút x5, x15, … gửi yêu cầu với BBRv1 |
| Chỉ số | handshake time, TTFB, download time, throughput (`curl --write-out`); CPU đo riêng |
| Starlink | gRPC mỗi giây, ghép với từng lượt tải theo dấu thời gian |

```
 Raspberry Pi 4 (trungnt@100.72.249.11)            AWS 47.129.174.173  (/data/starlink-cc)
 ┌───────────────────────────────┐  Starlink  ┌──────────────────────────────────────────┐
 │ curl (ngtcp2/nghttp2)         │ ─────────► │ ols-cubic  udp/4461 CUBIC + tcp/443 (h2)  │
 │   h2-tls12  h2-tls13  h3      │            │ ols-bbr    udp/4462 BBRv1                 │
 │ bench: runs / cpu / dish      │            │ tcp-cc.timer x4:30 bbr, x9:30 cubic (TCP) │
 │ gRPC 192.168.100.1:9200 1 Hz  │            │ ols-cpu    logs/cpu.csv (cgroup, softirq) │
 └───────────────────────────────┘            │ www/ 2MB.bin 50MB.bin manifest.json       │
                                              └──────────────────────────────────────────┘
```

## Cách thiết lập CCA

- **QUIC**: CCA chạy trong không gian người dùng (lsquic bên trong OpenLiteSpeed). OpenLiteSpeed
  chỉ cho đặt CCA của QUIC ở cấp server (`tuning.quicCongestionCtrl`: 1 = CUBIC, 2 = BBRv1), nên
  mỗi CCA có một instance OpenLiteSpeed riêng (container `litespeedtech/openlitespeed:1.8.5`,
  lsquic 4.4.1). Máy khách chọn CCA qua cổng đích.
- **TCP**: CCA là tham số của nhân Linux. Kernel gán CCA cho socket khi server chấp nhận kết nối,
  nên thay đổi chỉ áp dụng cho kết nối mới và mỗi lượt tải dùng CCA có hiệu lực lúc bắt tay.
  `tcp-cc.timer` đổi CCA đúng x4:30 và x9:30. Mỗi lần đổi được ghi vào `logs/tcp_cc.csv`, và log
  này dùng để kiểm chứng CCA của từng lượt tải TCP.
- Container chạy với `--network host`. Socket TCP vì vậy nằm trong network namespace của host,
  nơi timer đổi CCA, và trên đường dữ liệu không có NAT hay docker-proxy.
- **Chỉ một cổng TCP**: CCA của TCP do kernel gán cho socket lúc chấp nhận kết nối, không phụ
  thuộc instance hay cổng. Vì vậy HTTP/2 của cả hai CCA đi qua một listener duy nhất, `tcp/443`
  trên `ols-cubic`; cổng thứ hai sẽ luôn mang cùng CCA nên không phân biệt được gì. Trong cửa sổ
  BBRv1, lượt HTTP/3 gửi tới `ols-bbr` (`udp/4462`), còn lượt HTTP/2 vẫn tới `tcp/443` nhưng
  chạy BBRv1 vì timer đã đổi CCA của kernel.
- Mỗi lượt ghi cột `server` trong `runs.csv`: container đã phục vụ lượt đó (HTTP/2 luôn là
  `cubic`). CPU server của một lượt được lấy từ đúng container này.
- OpenLiteSpeed tự mở thêm socket TCP cùng số với listener QUIC (`tcp/4461`, `tcp/4462`). Không
  client nào dùng, và Security Group cũng chặn.
- **Vì sao dùng 443**: Security Group của AWS chỉ cho TCP vào cổng 22, 80, 443, còn UDP thì mở
  4461/4462. Để dùng 443, phải tạm dừng container `openlitespeed_server` của dự án
  videostreaming (container này chiếm cả 80 lẫn 443; restart policy `unless-stopped`, nên nó đứng
  yên kể cả khi reboot). Khôi phục: dừng OLS của thí nghiệm
  (`sudo systemctl disable --now ols@cubic ols@bbr ols-cpu`), sau đó
  `docker start openlitespeed_server`.
- **SNI bắt buộc**: lsquic tìm certificate cho QUIC theo SNI. Nếu client không gửi SNI, lsquic
  hủy bắt tay với `internal_error` (TLS alert 80); listener TCP thì tự dùng certificate mặc định.
  Vì vậy máy khách gọi server bằng tên trong certificate (`starlink-cc.test`, lấy từ manifest) và
  ghim tên này vào IP bằng `curl --resolve`: không tra DNS, và cả 3 ngăn xếp gửi cùng một SNI.

## Thiết kế thí nghiệm

- **Cửa sổ theo CCA**: mỗi cửa sổ mở 30 s sau lần đổi CCA và đóng ngay trước lần đổi kế tiếp
  (CUBIC: x0:00–x4:30, BBRv1: x5:00–x9:30, mỗi cửa sổ 4 phút 30 giây). Khoảng 30 s này hấp thụ độ lệch đồng hồ NTP giữa hai máy và cho
  hàng đợi kịp xả hết.
- **Batch**: trong mỗi cửa sổ, máy khách chạy mỗi tổ hợp ngăn xếp × kích thước đúng một lần
  (6 lượt), theo thứ tự ngẫu nhiên (seed được ghi lại). Một round gồm một cửa sổ CUBIC và một cửa
  sổ BBRv1, kéo dài 10 phút.
- **So sánh**: các ngăn xếp được so sánh trong cùng một cửa sổ (cùng điều kiện Starlink). Hai CCA
  được so sánh giữa hai cửa sổ liền kề, cách nhau 5 phút.
- **Khoảng nghỉ** `GAP_SECONDS` trước mỗi lượt.
- **Bắt tay đầy đủ cho mỗi lượt**: mỗi lượt là một kết nối mới. Không có session resumption,
  0-RTT hay session ticket (đã tắt ở server). Server đặt `tcp_no_metrics_save=1` để kết nối TCP
  không thừa hưởng ssthresh/RTT của kết nối trước tới cùng địa chỉ (mọi lượt tải đều đi ra từ một
  IP CGNAT của Starlink).
- **Cùng AEAD**: cả ba ngăn xếp đều dùng ChaCha20-Poly1305 (Pi 4 không có AES phần cứng).
- **Bỏ qua lượt muộn**: nếu bắt tay có thể kéo dài qua lần đổi CCA kế tiếp, lượt đó không chạy mà
  được ghi là skipped (`exit_code` rỗng). Lượt này không tính vào tỉ lệ hoàn thành.
- **Lượt hợp lệ** khi curl exit 0, HTTP 200, phiên bản HTTP đúng với ngăn xếp và số byte đúng như
  manifest. Lượt không hợp lệ vẫn được giữ (`valid=0`).

## Metric

| Metric | Nguồn | File |
|---|---|---|
| Handshake, TTFB, thời gian tải, throughput | `curl --write-out '%{json}'`: `time_appconnect` (→ `handshake_s`), `time_starttransfer` (→ `ttfb_s`), `time_total` (→ `download_time_s`), `speed_download` (→ `throughput_mbps`), kèm `time_connect`, `http_version`, `size_download` | `runs.csv` |
| CPU client | CPU time của process curl lấy từ `wait4()` rusage (`cpu_pct`, 100 = 1 core). Chuỗi 1 Hz: CPU toàn máy, CPU của curl, nhiệt độ SoC. Cờ `vcgencmd get_throttled` trước và sau mỗi lượt | `runs.csv`, `cpu.csv` |
| CPU server | `cpu.stat:usage_usec` của cgroup từng container, 1 Hz (100 = 1 vCPU), kèm `host_pct`, `softirq_pct`, `steal_pct` của host. Mỗi lượt lấy CPU của container trong cột `server` (HTTP/2 luôn là `ols-cubic`). Phần TCP chạy trong kernel (softirq) không được tính vào cgroup, nên phải xem thêm các cột của host | server `logs/cpu.csv` |
| CCA của TCP | log mỗi lần đổi: `ts, phase_s, scheduled, previous, effective` | server `logs/tcp_cc.csv` |
| Trạng thái dish | gRPC `get_status` 1 Hz: latency/drop tới PoP, throughput, obstruction, SNR, alert | `dish.csv`, `dish.jsonl` |

## Cấu trúc

| Thư mục | Chạy ở đâu | Nội dung |
|---|---|---|
| `server/` | server, đồng bộ tới `/data/starlink-cc/server` | `server.conf` (image, instance, lịch đổi CCA, payload), `setup.sh` (idempotent), `ols/` (mẫu cấu hình OpenLiteSpeed), `bin/` (`tcp_cc.py`, `cpu_monitor.py`, `make_manifest.py`), `systemd/` |
| `scripts/` | Pi, đồng bộ tới `~/starlink-cc` | `client.env`, `setup.sh`, `run.sh`, package `bench/` |
| `Makefile` | máy trạm | deploy, setup, lấy kết quả về |

Máy khách không giữ bản sao của bảng cổng → CCA hay lịch đổi CCA. Nó đọc `manifest.json` do
server sinh từ `server.conf`. Manifest gồm: cổng UDP của từng instance, listener TCP duy nhất
(`tcp: {instance, port}`), lịch, kích thước và SHA-256 của payload, digest
và phiên bản image, kernel, các sysctl của TCP/UDP.

## Chạy

```bash
make setup-server        # nạp tcp_bbr, kéo image OLS, tạo cert + payload + cấu hình + manifest, bật service
make setup-client        # venv (grpcio, protobuf), kiểm tra curl có HTTP2 + HTTP3 + ngtcp2

ssh trungnt@100.72.249.11
cd ~/starlink-cc
./run.sh dish                       # kiểm tra đọc được trạng thái dish
ROUNDS=1 ./run.sh run               # smoke test: 1 cửa sổ CUBIC + 1 cửa sổ BBRv1 = 12 lượt
tmux new -s cc './run.sh run'       # chạy đầy đủ (30 round ≈ 5 giờ, 360 lượt)

make fetch                          # trên máy trạm: kéo results/ của Pi và logs/ của server về
scripts/.venv/bin/python -m bench summarize results/client/<id> --server-logs results/server
```

Vận hành server: `systemctl status 'ols@*' ols-cpu tcp-cc.timer`, `systemctl list-timers tcp-cc.timer`,
`tail logs/tcp_cc.csv`, `docker logs ols-cubic`.

Trong lúc đo, kiểm tra một lượt TCP đang dùng CCA nào bằng
`ss -tin 'sport = :443'` (trong kết quả có chữ `cubic` hoặc `bbr`).

Sau khi đo xong, trả CCA của TCP về mặc định:
`sudo systemctl disable --now tcp-cc.timer && sudo sysctl -w net.ipv4.tcp_congestion_control=cubic`.

Lưu ý: việc đổi CCA của TCP áp dụng cho mọi kết nối TCP mới trong network namespace của host
(sshd, nginx:8443). Các container dùng mạng bridge riêng không bị ảnh hưởng.

## Kết quả (`results/<UTC>/`)

`meta.json` (config, manifest, ngăn xếp, trace kết nối có TLS version/cipher/ALPN cho từng ngăn xếp
và cổng, version curl/OS/kernel, sysctl TCP, route ra ngoài, NTP, seed) · `runs.csv` · `cpu.csv` ·
`dish.csv` · `dish.jsonl` · `summary.csv`. Mỗi ô (ngăn xếp × CCA × kích thước) trong
`summary.csv` gồm: median handshake/TTFB, median/P95 thời gian tải, P25/median/P75 throughput,
CPU client/server, latency PoP, drop rate, % lượt gặp vật cản, số lượt bị throttle, số lượt bị
steal, số lượt TCP sai CCA.
