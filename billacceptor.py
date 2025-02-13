import pigpio
import time
import datetime
import os

# 📌 Konfigurasi PIN GPIO
BILL_ACCEPTOR_PIN = 14  # Pin pulsa dari bill acceptor (DT)
EN_PIN = 15             # Pin enable untuk mengaktifkan bill acceptor

# 📌 Konfigurasi transaksi
TIMEOUT = 15  # Waktu maksimum transaksi sebelum cooldown (detik)
PULSE_TIMEOUT = 0.3  # Batas waktu antara pulsa untuk menentukan akhir transaksi (detik)
DEBOUNCE_TIME = 0.05  # 50ms debounce berdasarkan debug
MIN_PULSE_INTERVAL = 0.04  # 40ms minimum interval untuk menghindari pulsa ganda
TOLERANCE = 2  # Toleransi ±2 pulsa (hanya untuk nominal Rp. 2.000 ke atas)

# 📌 Mapping jumlah pulsa ke nominal uang
PULSE_MAPPING = {
    1: 1000,   # Tanpa toleransi
    2: 2000,   # Dengan toleransi ±2
    5: 5000,   # Dengan toleransi ±2
    10: 10000, # Dengan toleransi ±2
    20: 20000, # Dengan toleransi ±2
    50: 50000, # Dengan toleransi ±2
    100: 100000 # Dengan toleransi ±2
}

# 📌 Lokasi penyimpanan log
LOG_DIR = "/home/pi/logs"
LOG_FILE = os.path.join(LOG_DIR, "log.txt")

# 📌 Buat folder logs/ jika belum ada
if not os.path.exists(LOG_DIR):
    os.makedirs(LOG_DIR)
    print(f"📁 Folder log dibuat: {LOG_DIR}")  # 🔍 DEBUG

# 📌 Buat file log jika belum ada
if not os.path.exists(LOG_FILE):
    with open(LOG_FILE, "w") as log:
        log.write("=== LOG TRANSAKSI BILL ACCEPTOR ===\n")
    print(f"📝 File log dibuat: {LOG_FILE}")  # 🔍 DEBUG

# 📌 Fungsi Logging
def log_transaction(message):
    """ Fungsi untuk menulis log ke file """
    with open(LOG_FILE, "a") as log:
        timestamp = datetime.datetime.now().strftime("[%Y-%m-%d %H:%M:%S]")
        log.write(f"{timestamp} {message}\n")

# 📌 Variabel transaksi
pulse_count = 0
last_pulse_time = time.time()
last_transaction_time = time.time()
cooldown = True
total_amount = 0  # Akumulasi uang dalam sesi transaksi
first_transaction_time = None  # Waktu transaksi pertama kali

# 📌 Inisialisasi pigpio
pi = pigpio.pi()

if not pi.connected:
    log_transaction("⚠️ Gagal terhubung ke pigpio daemon! Pastikan pigpiod berjalan.")
    print("⚠️ Gagal terhubung ke pigpio daemon! Pastikan pigpiod berjalan.")  # 🔍 DEBUG
    exit()

pi.set_mode(BILL_ACCEPTOR_PIN, pigpio.INPUT)
pi.set_pull_up_down(BILL_ACCEPTOR_PIN, pigpio.PUD_UP)
pi.set_mode(EN_PIN, pigpio.OUTPUT)
pi.write(EN_PIN, 1)  # Awal: Aktifkan bill acceptor

print("🔧 Inisialisasi selesai. Menunggu transaksi...")  # 🔍 DEBUG
print("🟢 Bill acceptor siap menerima uang...")  # 🔍 DEBUG

def closest_valid_pulse(pulses):
    """ Koreksi jumlah pulsa dengan toleransi ±2 kecuali untuk Rp. 1000 """
    for valid_pulse, amount in PULSE_MAPPING.items():
        if valid_pulse == 1 and pulses == 1:
            return 1  # Rp. 1000 harus pas
        elif abs(pulses - valid_pulse) <= TOLERANCE:
            return valid_pulse
    return None  # Jika tidak sesuai nominal, anggap tidak valid

def count_pulse(gpio, level, tick):
    """ Callback untuk menangkap pulsa dari bill acceptor """
    global pulse_count, last_pulse_time, last_transaction_time, cooldown, total_amount, first_transaction_time

    current_time = time.time()
    interval = current_time - last_pulse_time

    if cooldown:
        print("🔄 Reset cooldown! Lanjutkan akumulasi uang.")  # 🔍 DEBUG
        cooldown = False
        first_transaction_time = datetime.datetime.now()
        log_transaction(f"🕒 Transaksi pertama kali dimulai pada {first_transaction_time}")

    if interval > DEBOUNCE_TIME and interval > MIN_PULSE_INTERVAL:
        pi.write(EN_PIN, 0)  # Nonaktifkan bill acceptor segera setelah uang masuk
        print("🛑 Bill acceptor dinonaktifkan sementara (uang masuk).")  # 🔍 DEBUG
        
        pulse_count += 1
        last_pulse_time = current_time
        last_transaction_time = current_time

        print(f"✅ Pulsa diterima! Interval: {round(interval, 3)} detik, Total pulsa: {pulse_count}")  # 🔍 DEBUG

# 📌 Callback untuk menangkap pulsa dari bill acceptor
pi.callback(BILL_ACCEPTOR_PIN, pigpio.RISING_EDGE, count_pulse)

try:
    while True:
        current_time = time.time()

        if not cooldown:
            print(f"⏳ Transaksi berjalan... Total saat ini: Rp.{total_amount}", end="\r")  # 🔍 DEBUG

        # 📌 Jika ada pulsa masuk dan lebih dari PULSE_TIMEOUT, anggap transaksi selesai
        if pulse_count > 0 and (current_time - last_pulse_time > PULSE_TIMEOUT):
            received_pulses = pulse_count
            pulse_count = 0  # Reset penghitung pulsa

            corrected_pulses = closest_valid_pulse(received_pulses)

            if corrected_pulses:
                received_amount = PULSE_MAPPING[corrected_pulses]
                total_amount += received_amount
                print(f"💰 Uang masuk: Rp.{received_amount} (Total sementara: Rp.{total_amount}) "
                      f"[Pulsa asli: {received_pulses}, Dikoreksi: {corrected_pulses}]")  # 🔍 DEBUG

                if corrected_pulses != received_pulses:
                    log_transaction(f"⚠️ Pulsa dikoreksi! Dari {received_pulses} ke {corrected_pulses}")
                    print(f"🔧 Pulsa dikoreksi dari {received_pulses} ke {corrected_pulses}")  # 🔍 DEBUG
                
                log_transaction(f"💰 Akumulasi transaksi pada {datetime.datetime.now()} : Rp.{total_amount}")
            else:
                print(f"⚠️ WARNING: Pulsa tidak valid ({received_pulses} pulsa). Transaksi dibatalkan.")  # 🔍 DEBUG
                log_transaction(f"⚠️ Pulsa tidak valid: {received_pulses}")

            pi.write(EN_PIN, 1)  # Aktifkan kembali bill acceptor
            print("🟢 Bill acceptor diaktifkan kembali dan siap menerima uang.")  # 🔍 DEBUG

        if not cooldown:
            remaining_time = TIMEOUT - (current_time - last_transaction_time)
            if remaining_time <= 0:
                log_transaction(f"🛑 Total akhir transaksi: Rp.{total_amount}")
                cooldown = True
                total_amount = 0

        time.sleep(0.1)

except KeyboardInterrupt:
    print("\n🛑 Program dihentikan oleh pengguna.")  # 🔍 DEBUG
    log_transaction("🛑 Program dihentikan oleh pengguna.")
    pi.stop()
except Exception as e:
    print(f"❌ ERROR: {str(e)}")  # 🔍 DEBUG
    log_transaction(f"❌ ERROR: {str(e)}")