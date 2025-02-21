import pigpio
import time
import datetime
import os
import requests
from flask import Flask, request, jsonify
import threading

# 📌 Konfigurasi PIN GPIO
BILL_ACCEPTOR_PIN = 14
EN_PIN = 15

# 📌 Konfigurasi transaksi
TIMEOUT = 15
DEBOUNCE_TIME = 0.05
TOLERANCE = 2

# 📌 Mapping jumlah pulsa ke nominal uang
PULSE_MAPPING = {
    1: 1000,
    2: 2000,
    5: 5000,
    10: 10000,
    20: 20000,
    50: 50000,
    100: 100000
}

# 📌 API URL
INVOICE_API = "https://api-dev.xpdisi.id/invoice/"
BILL_API = "https://api-dev.xpdisi.id/order/billacceptor"

# 📌 Lokasi penyimpanan log transaksi
LOG_DIR = "/var/www/html/logs"
LOG_FILE = os.path.join(LOG_DIR, "log.txt")

if not os.path.exists(LOG_DIR):
    os.makedirs(LOG_DIR)

def log_transaction(message):
    timestamp = datetime.datetime.now().strftime("[%Y-%m-%d %H:%M:%S]")
    with open(LOG_FILE, "a") as log:
        log.write(f"{timestamp} {message}\n")
    print(f"{timestamp} {message}")

# 📌 Inisialisasi Flask
app = Flask(__name__)

# 📌 Variabel Global
pulse_count = 0
last_pulse_time = time.time()
transaction_active = False
total_inserted = 0
id_trx = None
payment_token = None
product_price = 0
last_pulse_received_time = time.time()
timeout_thread = None  # 🔥 Simpan thread timeout agar tidak dobel

# 📌 Inisialisasi pigpio
pi = pigpio.pi()
if not pi.connected:
    log_transaction("⚠️ Gagal terhubung ke pigpio daemon!")
    exit()

pi.set_mode(BILL_ACCEPTOR_PIN, pigpio.INPUT)
pi.set_pull_up_down(BILL_ACCEPTOR_PIN, pigpio.PUD_UP)
pi.set_mode(EN_PIN, pigpio.OUTPUT)
pi.write(EN_PIN, 0)

# 📌 Fungsi GET ke API Invoice
def fetch_invoice_details(payment_token):
    try:
        response = requests.get(f"{INVOICE_API}{payment_token}", timeout=5)
        response_data = response.json()
        if response.status_code == 200 and "data" in response_data:
            invoice_data = response_data["data"]

            # 🔥 Hanya lanjutkan jika isPaid == False
            if not invoice_data.get("isPaid", False):
                log_transaction(f"✅ Invoice {payment_token} belum dibayar, transaksi dapat dilanjutkan.")
            else:
                log_transaction(f"🚫 Transaksi dibatalkan! Invoice {payment_token} sudah dibayar.")
                return None, None, None

            try:
                product_price = int(invoice_data["productPrice"])
            except (ValueError, TypeError):
                log_transaction(f"⚠️ Gagal mengonversi productPrice: {invoice_data['productPrice']}")
                return None, None, None

            return invoice_data["ID"], invoice_data["paymentToken"], product_price
    except requests.exceptions.RequestException as e:
        log_transaction(f"⚠️ Gagal mengambil data invoice: {e}")
    return None, None, None


# 📌 Fungsi untuk menghitung pulsa
def count_pulse(gpio, level, tick):
    global pulse_count, last_pulse_time, total_inserted, last_pulse_received_time, product_price, timeout_thread

    if not transaction_active:
        return

    current_time = time.time()
    if (current_time - last_pulse_time) > DEBOUNCE_TIME:
        pulse_count += 1
        last_pulse_time = current_time
        last_pulse_received_time = current_time  # 🔥 Reset waktu timeout saat uang masuk

        received_amount = PULSE_MAPPING.get(pulse_count, 0)
        if received_amount:
            total_inserted += received_amount
            remaining_due = max(product_price - total_inserted, 0)  # 🔥 Sisa tagihan
            log_transaction(f"💰 Uang masuk: Rp.{received_amount} | Total: Rp.{total_inserted} | Sisa: Rp.{remaining_due}")
            pulse_count = 0  # Reset count setelah log

            # 🔥 Cegah multiple timeout threads
            if timeout_thread is None or not timeout_thread.is_alive():
                timeout_thread = threading.Thread(target=start_timeout_timer, daemon=True)
                timeout_thread.start()

# 📌 Fungsi untuk menangani timeout & pembayaran sukses
def start_timeout_timer():
    global transaction_active, total_inserted, product_price, last_pulse_received_time, timeout_thread

    while transaction_active:
        remaining_time = TIMEOUT - int(time.time() - last_pulse_received_time)
        remaining_time = max(remaining_time, 0)  # 🔥 Pastikan tidak minus
        print(f"\r⏳ Timeout dalam {remaining_time} detik...", end="")
        time.sleep(1)

        remaining_due = max(product_price - total_inserted, 0)  # 🔥 Hitung sisa tagihan

        # 🔥 Perbarui timeout jika ada pulsa masuk (agar tidak langsung timeout)
        if time.time() - last_pulse_received_time < TIMEOUT:
            continue  # Timeout akan diperpanjang jika ada pulsa masuk

        # 🔥 Cek apakah pembayaran sudah cukup
        if total_inserted >= product_price:
            log_transaction(f"✅ Transaksi selesai! Total: Rp.{total_inserted} | Kembalian: Rp.{total_inserted - product_price}")
            send_transaction_status()
            reset_transaction()  # 🔥 Reset transaksi
            pi.write(EN_PIN, 0)  # Matikan EN_PIN setelah transaksi selesai
            break

        # 🔥 Jika timeout terjadi dan masih kurang uangnya
        transaction_active = False
        pi.write(EN_PIN, 0)
        log_transaction(f"⏰ Timeout! Total masuk: Rp.{total_inserted} | Sisa Tagihan: Rp.{remaining_due}")
        send_transaction_status()
        break

# 📌 Reset transaksi setelah selesai
def reset_transaction():
    global transaction_active, total_inserted, id_trx, payment_token, product_price, last_pulse_received_time
    transaction_active = False
    total_inserted = 0
    id_trx = None
    payment_token = None
    product_price = 0
    last_pulse_received_time = time.time()  # 🔥 Reset waktu terakhir pulsa diterima
    log_transaction("🔄 Transaksi di-reset ke default.")

# 📌 API untuk Memulai Transaksi
@app.route("/api/ba", methods=["POST"])
def trigger_transaction():
    global transaction_active, total_inserted, id_trx, payment_token, product_price, last_pulse_received_time

    if transaction_active:
        return jsonify({"status": "error", "message": "Transaksi sedang berlangsung"}), 400

    data = request.json
    payment_token = data.get("paymentToken")

    if not payment_token:
        return jsonify({"status": "error", "message": "Token pembayaran tidak valid"}), 400

    id_trx, payment_token, product_price = fetch_invoice_details(payment_token)

    if id_trx is None or product_price is None:
        return jsonify({"status": "error", "message": "Invoice tidak valid atau sudah dibayar"}), 400

    transaction_active = True
    last_pulse_received_time = time.time()  # 🔥 Reset waktu timeout saat transaksi dimulai
    log_transaction(f"🔔 Transaksi dimulai! ID: {id_trx}, Token: {payment_token}, Tagihan: Rp.{product_price}")
    pi.write(EN_PIN, 1)
    threading.Thread(target=start_timeout_timer, daemon=True).start()

    return jsonify({"status": "success", "message": "Transaksi dimulai"})

if __name__ == "__main__":
    pi.callback(BILL_ACCEPTOR_PIN, pigpio.RISING_EDGE, count_pulse)
    app.run(host="0.0.0.0", port=5000, debug=True)