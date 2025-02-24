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
TIMEOUT = 30
DEBOUNCE_TIME = 0.05
TOLERANCE = 2
MAX_RETRY = 2  # 🔥 Maksimal ulang 2 kali

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
INVOICE_API = "https://app.xpdisi.id/invoice/"
BILL_API = "https://app.xpdisi.id/order/billacceptor"

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
insufficient_payment_count = 0


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
            log_transaction(f"✅ Pembayaran sukses: {res_data.get('message')}, Waktu: {res_data.get('payment date')}")
            reset_transaction()  # 🔥 Reset transaksi setelah sukses

        elif response.status_code == 400:
            try:
                res_data = response.json()
                error_message = res_data.get("error") or res_data.get("message", "Error tidak diketahui")
            except ValueError:
                error_message = response.text  # Jika JSON tidak valid, gunakan respons mentah

            log_transaction(f"⚠️ Gagal ({response.status_code}): {error_message}")

            if "Insufficient payment" in error_message:
                global insufficient_payment_count
                insufficient_payment_count += 1  # 🔥 Tambah hitungan gagal

                if insufficient_payment_count > MAX_RETRY:
                    log_transaction("🚫 Pembayaran kurang dan telah melebihi toleransi transaksi, transaksi dibatalkan!")
                    reset_transaction()
                    pi.write(EN_PIN, 1)  # 🔥 Pastikan EN_PIN tetap menyala agar tetap menerima uang
                else:
                    log_transaction(f"🔄 Pembayaran kurang, percobaan {insufficient_payment_count}/{MAX_RETRY}. Lanjutkan memasukkan uang...")
                    last_pulse_received_time = time.time()  # 🔥 Reset timer agar timeout diperpanjang
                    transaction_active = True  # Pastikan transaksi tetap aktif
                    pi.write(EN_PIN, 1)  # 🔥 Pastikan EN_PIN tetap menyala agar tetap menerima uang
                    start_timeout_timer()

            elif "Payment already completed" in error_message:
                log_transaction("✅ Pembayaran sudah selesai sebelumnya. Reset transaksi.")
                pi.write(EN_PIN, 0)  # 🔥 Matikan EN_PIN setelah transaksi selesai

        else:
            log_transaction(f"⚠️ Respon tidak terduga: {response.status_code}")

    except requests.exceptions.RequestException as e:
        log_transaction(f"⚠️ Gagal mengirim status transaksi: {e}")
        
def closest_valid_pulse(pulses):
    """Mendapatkan jumlah pulsa yang paling mendekati nilai yang valid."""
    if pulses == 1:
        return 1
    if 2 < pulses < 5:
        return 2
    closest_pulse = min(PULSE_MAPPING.keys(), key=lambda x: abs(x - pulses) if x != 1 else float("inf"))
    return closest_pulse if abs(closest_pulse - pulses) <= TOLERANCE else None

# 📌 Fungsi untuk menghitung pulsa
def count_pulse(gpio, level, tick):
    """Menghitung pulsa dari bill acceptor dan mengonversinya ke nominal uang."""
    global pulse_count, last_pulse_time, total_inserted, last_pulse_received_time, product_price, timeout_thread

    if not transaction_active:
        return

    current_time = time.time()

    # Pastikan debounce
    if (current_time - last_pulse_time) > DEBOUNCE_TIME:
        pulse_count += 1
        last_pulse_time = current_time
        last_pulse_received_time = current_time  # *Cooldown reset setiap pulsa masuk*
        print(f"🔢 Pulsa diterima: {pulse_count}")  # Debugging

        # Konversi pulsa ke uang dengan koreksi pulsa
        corrected_pulses = closest_valid_pulse(pulse_count)
        if corrected_pulses:
            received_amount = PULSE_MAPPING.get(corrected_pulses, 0)
            total_inserted += received_amount
            remaining_due = max(product_price - total_inserted, 0)  # 🔥 Sisa tagihan
            print(f"\r💰 Total uang masuk: Rp.{total_inserted}", end="")
            log_transaction(f"💰 Uang masuk: Rp.{received_amount} | Total: Rp.{total_inserted} | Sisa: Rp.{remaining_due}")
            pulse_count = 0  # Reset count setelah log

            # 🔥 Cegah multiple timeout threads
            if timeout_thread is None or not timeout_thread.is_alive():
                timeout_thread = threading.Thread(target=start_timeout_timer, daemon=True)
                timeout_thread.start()

# 📌 Fungsi untuk menangani timeout & pembayaran sukses
def start_timeout_timer():
    """Mengatur timer untuk mendeteksi timeout transaksi."""
    global total_inserted, product_price, transaction_active, last_pulse_received_time, id_trx

    while transaction_active:
        current_time = time.time()
        remaining_time = max(0, int(TIMEOUT - (current_time - last_pulse_received_time)))  # Timeout dalam detik

        if remaining_time == 0:
            # *🔥 Timeout tercapai, hentikan transaksi*
            transaction_active = False
            pi.write(EN_PIN, 0)  # Matikan bill acceptor
            
            remaining_due = max(0, product_price - total_inserted)  # *Sisa pembayaran untuk log*
            overpaid = max(0, total_inserted - product_price)  # *Kelebihan pembayaran untuk log*

            if total_inserted < product_price:
                log_transaction(f"⏰ Timeout! Kurang: Rp.{remaining_due}")
            elif total_inserted == product_price:
                log_transaction(f"✅ Transaksi sukses, total: Rp.{total_inserted}")
            else:
                log_transaction(f"✅ Transaksi sukses, kelebihan: Rp.{overpaid}")

            # *🔥 Kirim status transaksi*
            send_transaction_status()

            break  # *Hentikan loop setelah timeout*

        # *Tampilkan waktu timeout di terminal*
        print(f"\r⏳ Timeout dalam {remaining_time} detik...", end="")
        time.sleep(1)

        # *🔥 Cek apakah cukup uang setelah 2 detik tanpa pulsa tambahan*
        if (current_time - last_pulse_received_time) >= 2 and total_inserted >= product_price:
            transaction_active = False
            pi.write(EN_PIN, 0)  # Matikan bill acceptor
            
            overpaid = max(0, total_inserted - product_price)  # 🔥 Ensure overpaid is set

            if total_inserted == product_price:
                log_transaction(f"✅ Transaksi selesai, total: Rp.{total_inserted}")
            else:
                log_transaction(f"✅ Transaksi selesai, kelebihan: Rp.{overpaid}")

            # *🔥 Kirim status transaksi*
            send_transaction_status()

            break  # *Hentikan loop setelah sukses*


# 📌 Reset transaksi setelah selesai
def reset_transaction():
    global transaction_active, total_inserted, id_trx, payment_token, product_price, last_pulse_received_time, insufficient_payment_count
    transaction_active = False
    total_inserted = 0
    id_trx = None
    payment_token = None
    product_price = 0
    last_pulse_received_time = time.time()  # 🔥 Reset waktu terakhir pulsa diterima
    insufficient_payment_count = 0  # 🔥 Reset penghitung pembayaran kurang
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