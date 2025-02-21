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

# 📌 Fungsi GET ke API Invoice (Tambahan pengecekan ispaid sebagai string)
def fetch_invoice_details(payment_token):
    try:
        response = requests.get(f"{INVOICE_API}{payment_token}", timeout=5)
        response_data = response.json()
        if response.status_code == 200 and "data" in response_data:
            invoice_data = response_data["data"]
            
            # 🔥 Pastikan pengecekan "ispaid" sebagai string ("true"/"false")
            is_paid = str(invoice_data.get("ispaid", "false")).lower().strip()  # Pastikan lowercase & trim
            
            # 🔥 Jika "ispaid" adalah "true", berarti sudah dibayar → transaksi dibatalkan
            if is_paid == "true":  
                log_transaction("⚠️ Transaksi dibatalkan: Invoice sudah dibayar sebelumnya.")
                return None, None, None, True  # ✅ True = sudah dibayar
            
            try:
                product_price = int(invoice_data["productPrice"])  # Pastikan huruf kecil sesuai API
            except (ValueError, TypeError):
                log_transaction(f"⚠️ Gagal mengonversi productprice: {invoice_data['productprice']}")
                return None, None, None, False

            return invoice_data["id"], invoice_data["paymenttoken"], product_price, False  # ✅ False = belum dibayar
    except requests.exceptions.RequestException as e:
        log_transaction(f"⚠️ Gagal mengambil data invoice: {e}")
    return None, None, None, False


# 📌 Fungsi POST hasil transaksi
def send_transaction_status():
    global total_inserted, transaction_active, last_pulse_received_time

    try:
        response = requests.post(BILL_API, json={
            "ID": id_trx,
            "paymentToken": payment_token,
            "productPrice": total_inserted  # Hanya mengirim total uang yang masuk
        }, timeout=5)

        if response.status_code == 200:
            res_data = response.json()
            log_transaction(f"✅ Pembayaran sukses: {res_data.get('message')}, Waktu: {res_data.get('paymentDate')}")
            reset_transaction()  # 🔥 Reset transaksi setelah sukses

        elif response.status_code == 400:
            try:
                res_data = response.json()
                error_message = res_data.get("error") or res_data.get("message", "Error tidak diketahui")
            except ValueError:
                error_message = response.text  # Jika JSON tidak valid, gunakan respons mentah

            log_transaction(f"⚠️ Gagal ({response.status_code}): {error_message}")

            if "Insufficient payment" in error_message:
                log_transaction("🔄 Pembayaran kurang, lanjutkan memasukkan uang...")
                last_pulse_received_time = time.time()  # 🔥 Reset timer agar timeout diperpanjang
                transaction_active = True  # Pastikan transaksi tetap aktif
                pi.write(EN_PIN, 1)  # 🔥 Pastikan EN_PIN tetap menyala agar tetap menerima uang
                start_timeout_timer()

            elif "Payment already completed" in error_message:
                log_transaction("✅ Pembayaran sudah selesai sebelumnya. Reset transaksi.")
                reset_transaction()  # 🔥 Jika sudah selesai, reset transaksi
                pi.write(EN_PIN, 0)  # 🔥 Matikan EN_PIN setelah transaksi selesai

        else:
            log_transaction(f"⚠️ Respon tidak terduga: {response.status_code}")

    except requests.exceptions.RequestException as e:
        log_transaction(f"⚠️ Gagal mengirim status transaksi: {e}")

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
    """Mengatur timer untuk mendeteksi timeout transaksi."""
    global total_inserted, remaining_balance, transaction_active, last_pulse_received_time, id_trx, remaining_due

    while transaction_active:
        current_time = time.time()
        remaining_time = max(0, int(TIMEOUT - (current_time - last_pulse_received_time)))  # **Integer timeout**
        
        if remaining_time == 0:
            # Timeout tercapai, matikan bill acceptor
            pi.write(EN_PIN, 0)
            transaction_active = False
            
            if total_inserted < remaining_balance:
                remaining_due = remaining_balance - total_inserted  # **Hitung sisa tagihan**
                print(f"\r⏰ Timeout! Kurang: Rp.{remaining_due}")
                log_transaction(f"⚠️ Transaksi gagal, kurang: Rp.{remaining_due}")
                send_transaction_status("failed", total_inserted, 0, remaining_due)  # **Kirim sebagai "failed" dengan sisa tagihan**
            elif total_inserted == remaining_balance:
                print(f"\r✅ Transaksi berhasil, total: Rp.{total_inserted}")
                log_transaction(f"✅ Transaksi berhasil, total: Rp.{total_inserted}")
                send_transaction_status("success", total_inserted, 0, 0)  # **Transaksi sukses**
            else:
                overpaid = total_inserted - remaining_balance
                print(f"\r✅ Transaksi berhasil, kelebihan: Rp.{overpaid}")
                log_transaction(f"✅ Transaksi berhasil, kelebihan: Rp.{overpaid}")
                send_transaction_status("overpaid", total_inserted, overpaid, 0)  # **Transaksi sukses, tapi kelebihan uang**
            
            reset_transaction()  # **Reset transaksi setelah timeout**
            break
        
        print(f"\r⏳ Timeout dalam {remaining_time} detik...", end="")  # **Tampilkan sebagai integer**
        time.sleep(1)

        # **Logika pengecekan setelah 2 detik**
        if current_time - last_pulse_received_time >= 2:
            if total_inserted >= remaining_balance:
                # Jika uang sudah cukup atau lebih, langsung kirim status transaksi dan hentikan transaksi
                overpaid = total_inserted - remaining_balance
                if total_inserted == remaining_balance:
                    print(f"\r✅ Transaksi selesai, total: Rp.{total_inserted}")
                    send_transaction_status("success", total_inserted, 0, 0)  # Transaksi sukses
                else:
                    print(f"\r✅ Transaksi selesai, kelebihan: Rp.{overpaid}")
                    send_transaction_status("overpaid", total_inserted, overpaid, 0)  # Transaksi sukses, tapi kelebihan uang
                
                transaction_active = False
                pi.write(EN_PIN, 0)  # Matikan bill acceptor
                reset_transaction()  # **Reset transaksi setelah sukses**
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
        return jsonify({"status": "error", "message": "Invoice tidak valid atau sudah dibayar"}), 400  # 🔥 Tambahkan error jika invoice sudah dibayar

    transaction_active = True
    last_pulse_received_time = time.time()  # 🔥 Reset waktu timeout saat transaksi dimulai
    log_transaction(f"🔔 Transaksi dimulai! ID: {id_trx}, Token: {payment_token}, Tagihan: Rp.{product_price}")
    pi.write(EN_PIN, 1)
    threading.Thread(target=start_timeout_timer, daemon=True).start()

    return jsonify({"status": "success", "message": "Transaksi dimulai"})

if __name__ == "__main__":
    pi.callback(BILL_ACCEPTOR_PIN, pigpio.RISING_EDGE, count_pulse)
    app.run(host="0.0.0.0", port=5000, debug=True)