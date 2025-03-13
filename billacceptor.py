import pigpio
import time
import datetime
import os
import requests
from flask import Flask, request, jsonify
import threading

# Konfigurasi PIN GPIO
BILL_ACCEPTOR_PIN = 14
EN_PIN = 15

# Konfigurasi transaksi
TIMEOUT = 15
DEBOUNCE_TIME = 0.05
TOLERANCE = 2
MAX_RETRY = 1

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

# Variabel Global
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
trigger_transaction_event = threading.Event()
processed_tokens = set()
timeout_event = threading.Event()  # Event untuk menghentikan thread
start_time = time.monotonic()  # Ambil waktu awal

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
pi.write(EN_PIN, 0)

# Fungsi GET ke API Invoice
def fetch_invoice_details():
    try:
        response = requests.get(INVOICE_API, timeout=5)
        response_data = response.json()

        if response.status_code == 200 and "data" in response_data:
            for invoice in response_data["data"]:
                if not invoice.get("isPaid", False):
                    log_transaction(f"✅ Invoice ditemukan: {invoice['paymentToken']}, belum dibayar.")
                    return invoice["ID"], invoice["paymentToken"], int(invoice["productPrice"])

        log_transaction("✅ Tidak ada invoice yang belum dibayar.")
    except requests.exceptions.RequestException as e:
        log_transaction(f"⚠️ Gagal mengambil data invoice: {e}")

    return None, None, None

# Fungsi POST hasil transaksi
def send_transaction_status():
    global total_inserted, transaction_active, last_pulse_received_time, insufficient_payment_count

    attempt = 0  # Tambahkan penghitung retry
    max_attempts = MAX_RETRY + 1  # Batas retry, termasuk percobaan awal

    while attempt < max_attempts:
        try:
            response = requests.post(BILL_API, json={
                "ID": id_trx,
                "paymentToken": payment_token,
                "productPrice": total_inserted
            }, timeout=10)  # Timeout lebih lama

            if response.status_code == 200:
                res_data = response.json()
                log_transaction(f"✅ Pembayaran sukses: {res_data.get('message')}, Waktu: {res_data.get('payment date')}")
                reset_transaction()  # Reset transaksi setelah sukses
                return
            elif response.status_code == 400:
                res_data = response.json() if response.content else {}
                error_message = res_data.get("error") or res_data.get("message", "Error tidak diketahui")

                log_transaction(f"⚠️ Gagal ({response.status_code}): {error_message}")

                if "Insufficient payment" in error_message:
                    insufficient_payment_count += 1

                    if insufficient_payment_count > MAX_RETRY:
                        log_transaction("🚫 Pembayaran kurang dan telah melebihi batas retry, transaksi dibatalkan!")
                        reset_transaction()
                        pi.write(EN_PIN, 1)

                        # Cari token baru setelah transaksi gagal
                        log_transaction("🔄 Kembali mencari token baru setelah transaksi gagal...")
                        trigger_transaction()
                    else:
                        log_transaction(f"🔄 Pembayaran kurang, percobaan {insufficient_payment_count}/{MAX_RETRY}. Silakan lanjutkan memasukkan uang...")

                        last_pulse_received_time = time.time()
                        transaction_active = True
                        pi.write(EN_PIN, 1)

                        # **Pastikan tidak ada timeout ganda**
                        if timeout_thread is not None:
                            timeout_event.set()  # Batalkan timeout sebelumnya sebelum memulai yang baru

                        start_timeout_timer()  # Mulai timeout baru
                        return  # **Pastikan kembali ke loop utama**


                elif "Payment already completed" in error_message:
                    log_transaction("✅ Pembayaran sudah selesai sebelumnya. Reset transaksi.")
                    pi.write(EN_PIN, 1)


                else:
                    log_transaction(f"⚠️ Error lain: {error_message}")

            else:
                log_transaction(f"⚠️ Respon tidak terduga: {response.status_code} - {response.text}")

        except requests.exceptions.RequestException as e:
            log_transaction(f"⚠️ Gagal mengirim status transaksi (percobaan {attempt + 1}/{max_attempts}): {e}")

        attempt += 1
        if attempt < max_attempts:
            log_transaction("🔄 Menunggu 2 detik sebelum mencoba kembali...")
            time.sleep(2)

    log_transaction("🚫 Gagal mengirim transaksi setelah semua percobaan. Reset transaksi!")
    reset_transaction()

    # **Pastikan loop kembali ke main dengan mencari transaksi baru**
    log_transaction("🔄 Mencari transaksi baru...")
    trigger_transaction()

def closest_valid_pulse(pulses):
    """Mendapatkan jumlah pulsa yang paling mendekati nilai yang valid."""
    if pulses == 1:
        return 1
    if 2 < pulses < 5:
        return 2
    closest_pulse = min(PULSE_MAPPING.keys(), key=lambda x: abs(x - pulses) if x != 1 else float("inf"))
    return closest_pulse if abs(closest_pulse - pulses) <= TOLERANCE else None

# Fungsi untuk menghitung pulsa
def count_pulse(gpio, level, tick):
    """Menghitung pulsa dari bill acceptor dan mengonversinya ke nominal uang."""
    global pulse_count, last_pulse_time, total_inserted, last_pulse_received_time, product_price, pending_pulse_count, timeout_thread

    if not transaction_active:
        return

    current_time = time.time()

    # Pastikan debounce
    if (current_time - last_pulse_time) > DEBOUNCE_TIME:
        if pending_pulse_count == 0:
            pi.write(EN_PIN, 0)
        pending_pulse_count += 1
        last_pulse_time = current_time
        last_pulse_received_time = current_time
        print(f"🔢 Pulsa diterima: {pending_pulse_count}")
        if timeout_thread is None or not timeout_thread.is_alive():
            timeout_thread = threading.Thread(target=start_timeout_timer, daemon=True)
            timeout_thread.start()

# Fungsi untuk menangani timeout & pembayaran sukses
def start_timeout_timer():
    """Memulai thread timeout hanya jika belum ada yang berjalan."""
    global timeout_thread, timeout_event

    with transaction_lock:
        if timeout_thread and timeout_thread.is_alive():
            timeout_event.set()  # Hentikan timeout lama sebelum membuat yang baru
            timeout_thread.join()  # Tunggu hingga thread lama berhenti
        
        timeout_event.clear()  # Reset event untuk timeout baru
        timeout_thread = threading.Thread(target=run_timeout_timer, daemon=True)
        timeout_thread.start()

def run_timeout_timer():
    """Thread timeout yang berjalan selama transaksi berlangsung."""
    global transaction_active, total_inserted, product_price, last_pulse_received_time, pending_pulse_count
    last_pulse_received_time = time.monotonic()  # Pastikan menggunakan monotonic time sebagai referensi

    while transaction_active:
        current_time = time.monotonic()
        remaining_time = max(0, int(TIMEOUT - (current_time - last_pulse_received_time)))

        # Proses pulsa jika tidak ada pulsa baru selama 2 detik
        if (current_time - last_pulse_received_time) >= 2 and pending_pulse_count > 0:
            process_final_pulse_count()
            continue  # Lanjutkan loop setelah pemrosesan

        # Jika jumlah uang cukup, transaksi selesai
        if total_inserted >= product_price:
            transaction_active = False
            pi.write(EN_PIN, 0)  # Matikan EN_PIN setelah transaksi

            overpaid = max(0, total_inserted - product_price)

            if total_inserted == product_price:
                log_transaction(f"✅ Transaksi selesai, total: Rp.{total_inserted}")
            else:
                log_transaction(f"✅ Transaksi selesai, kelebihan: Rp.{overpaid}")

            send_transaction_status()
            trigger_transaction()
            break  # Keluar dari loop setelah transaksi selesai

        # Timeout: transaksi gagal
        if remaining_time == 0:
            transaction_active = False
            pi.write(EN_PIN, 0)

            remaining_due = max(0, product_price - total_inserted)
            overpaid = max(0, total_inserted - product_price)

            if total_inserted < product_price:
                log_transaction(f"⏰ Timeout! Kurang: Rp.{remaining_due}")
            elif total_inserted == product_price:
                log_transaction(f"✅ Transaksi sukses, total: Rp.{total_inserted}")
            else:
                log_transaction(f"✅ Transaksi sukses, kelebihan: Rp.{overpaid}")

            send_transaction_status()
            trigger_transaction()
            break  # Keluar setelah timeout

        print(f"\r⏳ Timeout dalam {remaining_time} detik...", end="")
        time.sleep(1)

def process_final_pulse_count():
    """Memproses pulsa yang terkumpul setelah tidak ada pulsa masuk selama 2 detik."""
    global pending_pulse_count, total_inserted, pulse_count

    if pending_pulse_count == 0:
        return
    with transaction_lock:
        # Koreksi pulsa dengan toleransi ±2
        corrected_pulses = closest_valid_pulse(pending_pulse_count)

        if corrected_pulses:
            received_amount = PULSE_MAPPING.get(corrected_pulses, 0)
            total_inserted += received_amount
            remaining_due = max(product_price - total_inserted, 0)

            log_transaction(f"💰 Koreksi pulsa: {pending_pulse_count} -> {corrected_pulses} ({received_amount}) | Total: Rp.{total_inserted} | Sisa: Rp.{remaining_due}")

        else:
            log_transaction(f"⚠️ Pulsa {pending_pulse_count} tidak valid!")

        pending_pulse_count = 0  # Reset setelah diproses
        pi.write(EN_PIN, 1)  # Hidupkan kembali EN_PIN setelah koreksi
        log_transaction("✅ Koreksi selesai, EN_PIN diaktifkan kembali")

# Reset transaksi setelah selesai
def reset_transaction():
    global transaction_active, total_inserted, id_trx, payment_token, product_price, last_pulse_received_time, insufficient_payment_count, pending_pulse_count
    transaction_active = False
    total_inserted = 0
    id_trx = None
    payment_token = None
    product_price = 0
    last_pulse_received_time = time.monotonic()
    insufficient_payment_count = 0
    pending_pulse_count = 0
    log_transaction("🔄 Transaksi di-reset ke default.")

@app.route('/api/status', methods=['GET'])
def get_bill_acceptor_status():
    global transaction_active

    if transaction_active:
        return jsonify({
            "status": "error",
            "message": "Bill acceptor sedang dalam transaksi"
        }), 409

    return jsonify({
        "status": "success",
        "message": "Bill acceptor siap digunakan"
    }), 200
def trigger_transaction():
    global transaction_active, total_inserted, id_trx, payment_token, product_price, last_pulse_received_time, pending_pulse_count

    if trigger_transaction_event.is_set():
        log_transaction("[DEBUG] Thread trigger_transaction sudah berjalan, tidak membuat ulang.")
        return

    trigger_transaction_event.set()

    while True:
        if transaction_active:
            log_transaction("[DEBUG] Transaksi sedang berlangsung, menunggu transaksi selesai...")
            time.sleep(3)
            continue

        log_transaction("🔍 Mencari payment token terbaru...")

        try:
            response = requests.get(TOKEN_API, timeout=5)
            response_data = response.json()

            if response.status_code == 200 and "data" in response_data:
                for token_data in response_data["data"]:
                    created_time = datetime.datetime.strptime(token_data["CreatedAt"], "%Y-%m-%dT%H:%M:%S.%fZ")
                    created_time = created_time.replace(tzinfo=datetime.timezone.utc)
                    age_in_minutes = (datetime.datetime.now(datetime.timezone.utc) - created_time).total_seconds() / 60

                    payment_token = token_data["PaymentToken"]

                    if transaction_active:
                        log_transaction("[DEBUG] Transaksi masih aktif, menunggu...")
                        return

                    if payment_token in processed_tokens:
                        log_transaction(f"⚠️ Token {payment_token} sudah diproses, tidur 3 detik sebelum mencari lagi...")
                        time.sleep(3)
                        continue

                    if age_in_minutes <= 3:
                        log_transaction(f"[DEBUG] Token ditemukan: {payment_token}, umur: {age_in_minutes:.2f} menit")

                        invoice_response = requests.get(f"{INVOICE_API}{payment_token}", timeout=5)
                        invoice_data = invoice_response.json()

                        if invoice_response.status_code == 200 and "data" in invoice_data:
                            invoice = invoice_data["data"]
                            if not invoice.get("isPaid", False):
                                id_trx = invoice["ID"]
                                product_price = int(invoice["productPrice"])

                                transaction_active = True
                                pending_pulse_count = 0
                                last_pulse_received_time = time.time()
                                log_transaction(f"🔔 Transaksi dimulai! ID: {id_trx}, Token: {payment_token}, Tagihan: Rp.{product_price}")

                                processed_tokens.add(payment_token)
                                pi.write(EN_PIN, 1)
                                start_timeout_timer()

                            else:
                                log_transaction(f"⚠️ Invoice {payment_token} sudah dibayar, mencari lagi...")

            log_transaction("[DEBUG] Tidak ada token baru, tidur selama 5 detik...")
            time.sleep(5)

        except requests.exceptions.RequestException as e:
            log_transaction(f"⚠️ ERROR: {e}")
            time.sleep(1)

        finally:
            trigger_transaction_event.clear()  # Hapus event agar bisa mencari token baru
            log_transaction("[DEBUG] Thread trigger_transaction selesai, kembali mencari token.")

if __name__ == "__main__":
    pi.callback(BILL_ACCEPTOR_PIN, pigpio.RISING_EDGE, count_pulse)

    if not trigger_transaction_event.is_set():
        log_transaction("[DEBUG] Memulai thread trigger_transaction")
        transaction_thread = threading.Thread(target=trigger_transaction, daemon=True, name="TransactionThread")
        transaction_thread.start()

    app.run(host="0.0.0.0", port=5000, debug=True)