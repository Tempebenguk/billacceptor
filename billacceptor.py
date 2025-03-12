import pigpio
import time
import datetime
import os
import requests
from flask import Flask, request, jsonify
import threading

# Konfigurasi PIN GPIO
BILL_ACCEPTOR_PIN = 14
EN_PIN = 15  # Asumsi: Aktif HIGH untuk menonaktifkan bill acceptor

# Konfigurasi transaksi
TIMEOUT = 180
DEBOUNCE_TIME = 0.05
TOLERANCE = 2
MAX_RETRY = 2

# Mapping jumlah pulsa ke nominal uang
PULSE_MAPPING = {
    1: 1000,
    2: 2000,
    5: 5000,
    10: 10000,
    20: 20000,
    50: 50000,
    100: 100000
}

# API URL
TOKEN_API = "https://api-dev.xpdisi.id/invoice/device/bic01"
INVOICE_API = "https://api-dev.xpdisi.id/invoice/"
BILL_API = "https://api-dev.xpdisi.id/order/billacceptor"

# Lokasi penyimpanan log transaksi
LOG_DIR = "/var/www/html/logs"
LOG_FILE = os.path.join(LOG_DIR, "log.txt")

if not os.path.exists(LOG_DIR):
    os.makedirs(LOG_DIR)

# Inisialisasi Flask
app = Flask(__name__)

# Variabel Global dengan thread lock
pulse_count = 0
pending_pulse_count = 0
last_pulse_time = time.time()
transaction_active = False
total_inserted = 0
id_trx = None
payment_token = None
product_price = 0
last_pulse_received_time = time.time()
timeout_thread = None
insufficient_payment_count = 0
transaction_lock = threading.Lock()
log_lock = threading.Lock()

# Fungsi log transaction
def log_transaction(message):
    timestamp = datetime.datetime.now().strftime("[%Y-%m-%d %H:%M:%S]")
    with log_lock:
        with open(LOG_FILE, "a") as log:
            log.write(f"{timestamp} {message}\n")
        print(f"{timestamp} {message}")

# Inisialisasi pigpio
pi = pigpio.pi()
if not pi.connected:
    log_transaction("⚠️ Gagal terhubung ke pigpio daemon!")
    exit()

pi.set_mode(BILL_ACCEPTOR_PIN, pigpio.INPUT)
pi.set_pull_up_down(BILL_ACCEPTOR_PIN, pigpio.PUD_UP)
pi.set_mode(EN_PIN, pigpio.OUTPUT)
pi.write(EN_PIN, 1)  # Nonaktifkan bill acceptor awal

# Fungsi GET ke API Invoice
def fetch_invoice_details():
    try:
        response = requests.get(INVOICE_API, timeout=5)
        response.raise_for_status()
        response_data = response.json()

        if "data" in response_data:
            for invoice in response_data["data"]:
                if not invoice.get("isPaid", False):
                    return invoice["ID"], invoice["paymentToken"], int(invoice["productPrice"])
        log_transaction("✅ Tidak ada invoice yang belum dibayar.")
    except Exception as e:
        log_transaction(f"⚠️ Gagal mengambil data invoice: {str(e)}")
    return None, None, None

# Fungsi POST hasil transaksi
def send_transaction_status():
    global total_inserted, insufficient_payment_count
    try:
        response = requests.post(BILL_API, json={
            "ID": id_trx,
            "paymentToken": payment_token,
            "productPrice": total_inserted
        }, timeout=5)
        response.raise_for_status()
        res_data = response.json()
        log_transaction(f"✅ Pembayaran sukses: {res_data.get('message')}")
        reset_transaction()
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 400:
            error_msg = e.response.json().get('error', 'Pembayaran kurang')
            handle_payment_error(error_msg)
        else:
            log_transaction(f"⚠️ Error HTTP: {str(e)}")
    except Exception as e:
        log_transaction(f"⚠️ Gagal mengirim status transaksi: {str(e)}")
    finally:
        reset_transaction()

def handle_payment_error(error_msg):
    global insufficient_payment_count
    if "Insufficient payment" in error_msg:
        insufficient_payment_count += 1
        if insufficient_payment_count > MAX_RETRY:
            log_transaction("🚫 Melebihi batas percobaan pembayaran!")
            pi.write(EN_PIN, 1)
        else:
            log_transaction(f"🔄 Percobaan {insufficient_payment_count}/{MAX_RETRY}")
            start_transaction()
    else:
        log_transaction(f"⚠️ Error: {error_msg}")

# Koreksi pulsa dengan toleransi
def closest_valid_pulse(pulses):
    for valid in sorted(PULSE_MAPPING.keys(), reverse=True):
        if abs(valid - pulses) <= TOLERANCE:
            return valid
    return None

# Callback untuk pulsa
def count_pulse(gpio, level, tick):
    global pending_pulse_count, last_pulse_time, last_pulse_received_time
    current_time = time.time()
    if (current_time - last_pulse_time) > DEBOUNCE_TIME:
        with transaction_lock:
            pending_pulse_count += 1
            last_pulse_received_time = current_time
            pi.write(EN_PIN, 1)  # Nonaktifkan sementara
            log_transaction(f"🔢 Pulsa diterima: {pending_pulse_count}")
        last_pulse_time = current_time

# Proses pulsa dan timeout
def process_pulses():
    global pending_pulse_count, total_inserted
    with transaction_lock:
        if pending_pulse_count == 0:
            return
        corrected = closest_valid_pulse(pending_pulse_count)
        if corrected:
            amount = PULSE_MAPPING[corrected]
            total_inserted += amount
            log_transaction(f"💰 Ditambahkan: Rp{amount} | Total: Rp{total_inserted}")
        pending_pulse_count = 0
        pi.write(EN_PIN, 0)  # Aktifkan kembali

def start_timeout_timer():
    global transaction_active
    start_time = time.time()
    while transaction_active and (time.time() - start_time < TIMEOUT):
        if (time.time() - last_pulse_received_time) > 2:
            process_pulses()
        if total_inserted >= product_price:
            break
        time.sleep(1)
    if transaction_active:
        finalize_transaction()

def finalize_transaction():
    global transaction_active
    with transaction_lock:
        transaction_active = False
        pi.write(EN_PIN, 1)
        if total_inserted >= product_price:
            log_transaction("✅ Pembayaran berhasil!")
        else:
            log_transaction("⏰ Waktu habis, pembayaran kurang")
        send_transaction_status()

@app.route('/api/status', methods=['GET'])
def get_status():
    return jsonify({
        "status": "active" if transaction_active else "inactive",
        "total": total_inserted
    }), 200

def start_transaction():
    global transaction_active, id_trx, payment_token, product_price
    with transaction_lock:
        id_trx, payment_token, product_price = fetch_invoice_details()
        if id_trx:
            transaction_active = True
            pi.write(EN_PIN, 0)
            threading.Thread(target=start_timeout_timer).start()

def monitor_invoices():
    while True:
        if not transaction_active:
            start_transaction()
        time.sleep(5)

if __name__ == "__main__":
    pi.callback(BILL_ACCEPTOR_PIN, pigpio.RISING_EDGE, count_pulse)
    threading.Thread(target=monitor_invoices, daemon=True).start()
    app.run(host="0.0.0.0", port=5000, use_reloader=False)