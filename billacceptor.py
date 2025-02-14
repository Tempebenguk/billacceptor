import pigpio
import time
import datetime
import os
import requests

# 📌 Konfigurasi PIN GPIO
BILL_ACCEPTOR_PIN = 14  # Pin untuk mendeteksi pulsa dari bill acceptor
EN_PIN = 15  # Pin untuk mengaktifkan/deaktivasi bill acceptor

# 📌 Konfigurasi transaksi
TIMEOUT = 15  # Waktu maksimum sebelum transaksi dianggap selesai (detik)
PULSE_TIMEOUT = 0.3  # Waktu tunggu setelah pulsa terakhir diterima (detik)
DEBOUNCE_TIME = 0.05  # Waktu debounce untuk mencegah pulsa ganda (detik)
MIN_PULSE_INTERVAL = 0.04  # Interval minimum antar pulsa yang valid (detik)
TOLERANCE = 2  # Toleransi koreksi pulsa untuk mencegah kesalahan deteksi

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

# 📌 Lokasi penyimpanan log
LOG_DIR = "/var/www/html/logs"
LOG_FILE = os.path.join(LOG_DIR, "log.txt")

if not os.path.exists(LOG_DIR):
    os.makedirs(LOG_DIR)

if not os.path.exists(LOG_FILE):
    with open(LOG_FILE, "w") as log:
        log.write("=== LOG TRANSAKSI BILL ACCEPTOR ===\n")

# 📌 Fungsi Logging
def log_transaction(message, to_log=True):
    timestamp = datetime.datetime.now().strftime("[%Y-%m-%d %H:%M:%S]")
    log_msg = f"{timestamp} {message}"
    print(log_msg)
    if to_log:
        with open(LOG_FILE, "a") as log:
            log.write(log_msg + "\n")

# 📌 Variabel transaksi
pulse_count = 0
last_pulse_time = time.time()
last_transaction_time = time.time()
cooldown = True
total_amount = 0
transaction_completed = False

# 📌 Inisialisasi pigpio
pi = pigpio.pi()
if not pi.connected:
    log_transaction("⚠️ Gagal terhubung ke pigpio daemon! Pastikan pigpiod berjalan.")
    exit()

pi.set_mode(BILL_ACCEPTOR_PIN, pigpio.INPUT)
pi.set_pull_up_down(BILL_ACCEPTOR_PIN, pigpio.PUD_UP)
pi.set_mode(EN_PIN, pigpio.OUTPUT)
pi.write(EN_PIN, 1)

# 📌 Fungsi koreksi pulsa
def closest_valid_pulse(pulses):
    if pulses == 1:
        return 1  
    if 2 < pulses < 5:
        return 2  
    closest_pulse = None
    min_diff = float("inf")
    for valid_pulse in PULSE_MAPPING.keys():
        if valid_pulse != 1 and abs(pulses - valid_pulse) <= TOLERANCE:
            diff = abs(pulses - valid_pulse)
            if diff < min_diff:
                min_diff = diff
                closest_pulse = valid_pulse
    return closest_pulse

# 📌 Callback pulsa
def count_pulse(gpio, level, tick):
    global pulse_count, last_pulse_time, last_transaction_time, cooldown, transaction_completed
    current_time = time.time()
    interval = current_time - last_pulse_time
    if cooldown:
        cooldown = False
        transaction_completed = False
        log_transaction("🕒 Transaksi dimulai")  # ✅ Hanya log sekali
    if interval > DEBOUNCE_TIME and interval > MIN_PULSE_INTERVAL:
        pi.write(EN_PIN, 0)
        pulse_count += 1
        last_pulse_time = current_time
        last_transaction_time = current_time
        print(f"✅ Pulsa diterima! Interval: {round(interval, 3)} detik, Total pulsa: {pulse_count}")

pi.callback(BILL_ACCEPTOR_PIN, pigpio.RISING_EDGE, count_pulse)
print("✅ Bill acceptor siap menerima uang...")

# 📌 Fungsi kirim data ke PHP
def send_to_php(received_amount, total_amount):
    url = "http://localhost/web_billacceptor/index.php"
    data = {"received_amount": received_amount, "total_amount": total_amount}
    try:
        response = requests.post(url, data=data)
        if response.status_code == 200:
            log_transaction("✅ Data berhasil dikirim ke PHP!")
        else:
            log_transaction(f"⚠️ Gagal mengirim data ke PHP! Status code: {response.status_code}")
    except Exception as e:
        log_transaction(f"❌ ERROR saat mengirim data ke PHP: {str(e)}")

# 📌 Loop utama
try:
    while True:
        current_time = time.time()
        if pulse_count > 0 and (current_time - last_pulse_time > PULSE_TIMEOUT):
            received_pulses = pulse_count
            pulse_count = 0  
            corrected_pulses = closest_valid_pulse(received_pulses)
            if corrected_pulses:
                received_amount = PULSE_MAPPING[corrected_pulses]
                total_amount += received_amount

                log_message = f"💰 Uang masuk: Rp.{received_amount} (Total: Rp.{total_amount}) [Pulsa asli: {received_pulses}, Dikoreksi: {corrected_pulses}]"
                log_transaction(log_message)  # ✅ Hanya satu log
                print(log_message)

                if not transaction_completed:  # ✅ Data hanya dikirim sekali ke PHP
                    send_to_php(received_amount, total_amount)
                    transaction_completed = True

        if not cooldown:
            remaining_time = TIMEOUT - (current_time - last_transaction_time)
            if remaining_time > 0:
                print(f"⏳ Cooldown sisa {int(remaining_time)} detik...", end="\r", flush=True)
            else:
                print(f"\n🛑 Transaksi selesai! Total akhir: Rp.{total_amount}")  # 🔍 DEBUG
                log_transaction(f"🛑 Transaksi selesai! Total akhir: Rp.{total_amount}")  # ✅ Hanya satu log
                cooldown = True
                total_amount = 0  # Reset total setelah dicatat
                print("🔄 Bill acceptor siap menerima transaksi baru...")
        time.sleep(0.1)
except KeyboardInterrupt:
    log_transaction("🛑 Program dihentikan oleh pengguna.")
    pi.stop()
except Exception as e:
    log_transaction(f"❌ ERROR: {str(e)}")
    pi.stop()