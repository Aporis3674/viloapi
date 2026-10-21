# viloapi

نصب‌کنندهٔ Xray و API مدیریت سرور برای اتصال به ربات ViloShop. این مخزن رابط وب مدیریتی ندارد؛ مدیریت از ربات انجام می‌شود.

## پیش‌نیازها

- VPS خالی Ubuntu 22.04+ یا Debian 12+ با systemd؛ معماری amd64 یا arm64.
- دسترسی root، دامنه با رکورد A مستقیم به IPv4 عمومی سرور؛ پروکسی CDN خاموش.
- ورودی TCP پورت‌های 80، 443 و 8443 باز؛ دسترسی خروجی به GitHub و Let's Encrypt.
- روی سرور فعلی دارای مشتری اجرا نکنید. نصب‌کننده از بازنویسی نصب شناخته‌شده جلوگیری می‌کند؛ مهاجرت نصب‌های دیگر خودکار نیست.

## نصب

دامنه و ایمیل نمونه را جایگزین کنید. دستور فایل را در پوشه موقت مستقل دانلود می‌کند و فقط پس از دانلود موفق اجرا می‌کند:

```bash
work=$(mktemp -d) && curl -fsSL --proto '=https' --proto-redir '=https' https://raw.githubusercontent.com/Aporis3674/viloapi/main/scripts/install_node.sh -o "$work/install_node.sh" && sudo bash "$work/install_node.sh" --host vpn.example.com --email admin@example.com --country Germany --name Germany-2
```

کد دانلودشده با دسترسی root اجرا می‌شود؛ پیش از اجرای آن کد را بازبینی کنید. برای نصب تکرارپذیر، `main` در URL را با SHA کامیت مورد نظر جایگزین کنید و همان SHA را با `sudo env VILO_REF=<SHA> bash ...` به نصب‌کننده بدهید.

خروجی نصب یک توکن `vilo1.` است. توکن **محرمانه** است؛ Base64 رمزنگاری نیست. آن را فقط در گفتگوی خصوصی ربات، بخش `/sub` ← افزودن سرور جدید وارد کنید. توکن یا فایل `/etc/vilo-node/node.json` را منتشر نکنید.

## اجزا

- VLESS Reality روی 443؛ مسیر جایگزین مستقل WebSocket روی 80 (`/vilo-ws`). تغییر خودکار مسیر در کلاینت پیاده‌سازی نشده است.
- HTTPS مدیریت و ساب روی 8443 با گواهی Let's Encrypt.
- StatsService روی `127.0.0.1:10085`، API داخلی روی `127.0.0.1:10086`، ورودی WS داخلی روی `127.0.0.1:10087`.
- سرویس‌های `vilo-xray` و `vilo-node`؛ ذخیرهٔ مصرف در SQLite؛ بررسی حجم و انقضا هر ۱۰ ثانیه.
- خروجی ساب استاندارد Base64، Clash و sing-box.

## بررسی سرویس‌ها

```bash
sudo systemctl status vilo-xray vilo-node nginx --no-pager
```

```bash
sudo journalctl -u vilo-node -u vilo-xray -n 80 --no-pager
```

## تست محلی

```bash
python3 -I tests/node_agent_test.py
```

```bash
bash -n scripts/install_node.sh
```

تست‌ها از Xray و systemd شبیه‌سازی‌شده استفاده می‌کنند. نصب کامل روی VPS واقعی هنوز تأیید نشده است.

## محدودیت‌های فعلی

- اعمال تغییر فهرست کلاینت‌ها Xray را restart می‌کند؛ اتصال‌های جاری ممکن است لحظه‌ای قطع شوند.
- `userLimit` ذخیره می‌شود اما محدودیت تعداد دستگاه/IP همزمان هنوز اعمال نمی‌شود.
- وضعیت آنلاین از افزایش شمارنده ترافیک تخمین زده می‌شود، نه handshake واقعی؛ ISP شناسایی نمی‌شود.
- WS روی پورت 80 لایه TLS ندارد؛ برای انتقال حساس از Reality استفاده کنید.
- نصب ناموفق ممکن است فایل‌های نیمه‌کاره باقی بگذارد؛ اجرای مجدد آن‌ها را خودکار حذف نمی‌کند. قبل از ترمیم بررسی و پشتیبان‌گیری لازم است.

## منابع اتصال

[مخزن PattNG](https://github.com/patterniha/PattNG) و [Patt’s Channel](https://t.me/patt_channel_x/166) منابع معرفی‌شده برای روش‌های اتصال هستند. طبق گزارش ارسالی ۲۰۲۶-۱۰-۲۱، ECH روی `cloudflare-ech.com` مسدود، F&F روی فایروال ایرانسل فعال و WARP MASQUE-HTTP/2 قابل اتصال گزارش شده است. این گزارش‌ها آزمون مستقیم این نصب‌کننده نیستند. MASQUE/Aether و F&F در این نسخه نصب‌کننده پیاده‌سازی نشده‌اند؛ تغییر fingerprint به‌تنهایی پیاده‌سازی F&F نیست.
