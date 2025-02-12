import pigpio
import time
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

# 📌 Variabel transaksi
pulse_count = 0
last_pulse_time = time.time()
last_transaction_time = time.time()
cooldown = True
total_amount = 0  # Akumulasi uang dalam sesi transaksi
log_directory = "/home/eksan/billacceptor/logs"  # Direktori untuk menyimpan log
log_file = os.path.join(log_directory, "log.txt")

# 📌 Pastikan direktori log ada
os.makedirs(log_directory, exist_ok=True)

# 📌 Inisialisasi pigpio
pi = pigpio.pi()

if not pi.connected:
    log_transaction("⚠️ Gagal terhubung ke pigpio daemon! Pastikan pigpiod berjalan.")
    print("⚠️ Gagal terhubung ke pigpio daemon! Pastikan pigpiod berjalan.")
    exit()

pi.set_mode(BILL_ACCEPTOR_PIN, pigpio.INPUT)
pi.set_pull_up_down(BILL_ACCEPTOR_PIN, pigpio.PUD_UP)
pi.set_mode(EN_PIN, pigpio.OUTPUT)
pi.write(EN_PIN, 1)  # Awal: Aktifkan bill acceptor

def log_transaction(message):
    """ Menyimpan log transaksi ke file """
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(log_file, "a") as f:
        f.write(f"[{timestamp}] {message}\n")

def closest_valid_pulse(pulses):
    """ Menyesuaikan jumlah pulsa dengan nominal uang terdekat menggunakan toleransi hanya untuk Rp. 2.000 ke atas """
    for valid_pulse, amount in PULSE_MAPPING.items():
        if valid_pulse == 1:  # Rp. 1.000 harus pas
            if pulses == 1:
                return 1
        else:  # Pecahan Rp. 2.000 ke atas pakai toleransi ±2
            if abs(pulses - valid_pulse) <= TOLERANCE:
                return valid_pulse
    return None  # Jika terlalu jauh dari nominal yang valid, anggap tidak valid

def count_pulse(gpio, level, tick):
    """ Callback untuk menangkap pulsa dari bill acceptor. """
    global pulse_count, last_pulse_time, last_transaction_time, cooldown, total_amount

    try:
        current_time = time.time()
        interval = current_time - last_pulse_time  # Hitung jarak antar pulsa

        if cooldown:
            log_transaction("🔄 Reset cooldown! Lanjutkan akumulasi uang.")
            cooldown = False

        if interval > DEBOUNCE_TIME and interval > MIN_PULSE_INTERVAL:
            pi.write(EN_PIN, 0)  # Nonaktifkan bill acceptor segera setelah uang masuk
            pulse_count += 1
            last_pulse_time = current_time
            last_transaction_time = current_time  # Reset timer transaksi
    except Exception as e:
        log_transaction(f"❌ ERROR: Terjadi kesalahan pada count_pulse: {str(e)}")

# 📌 Callback untuk menangkap pulsa dari bill acceptor
pi.callback(BILL_ACCEPTOR_PIN, pigpio.RISING_EDGE, count_pulse)

print("🟢 Bill acceptor siap menerima uang...")
log_transaction("🟢 Bill acceptor siap menerima uang...")

try:
    while True:
        current_time = time.time()

        if pulse_count > 0 and (current_time - last_pulse_time > PULSE_TIMEOUT):
            received_pulses = pulse_count
            pulse_count = 0  # Reset penghitung pulsa untuk sesi berikutnya

            corrected_pulses = closest_valid_pulse(received_pulses)
            
            if corrected_pulses:
                received_amount = PULSE_MAPPING[corrected_pulses]
                total_amount += received_amount
                
                if corrected_pulses != received_pulses:
                    log_transaction(f"⚠️ Pulsa dikoreksi! Dari {received_pulses} ke {corrected_pulses}")
                
                log_transaction(f"💰 Uang masuk: Rp.{received_amount} (Total: Rp.{total_amount})")
            else:
                log_transaction(f"⚠️ WARNING: Pulsa tidak valid ({received_pulses} pulsa). Transaksi dibatalkan.")
                print(f"⚠️ WARNING: Pulsa tidak valid ({received_pulses} pulsa). Transaksi dibatalkan.")

            pi.write(EN_PIN, 1)  # Aktifkan kembali bill acceptor setelah transaksi selesai

        if not cooldown:
            remaining_time = TIMEOUT - (current_time - last_transaction_time)
            if remaining_time <= 0:
                log_transaction(f"🛑 Total akhir transaksi: Rp.{total_amount}")
                print("\n==========================")
                print(f"🛑 Total akhir: Rp.{total_amount}")
                print("🔻 Perangkat masuk cooldown...")
                print("==========================")
                cooldown = True
                total_amount = 0  # Reset jumlah uang setelah cooldown selesai

        time.sleep(0.1)

except KeyboardInterrupt:
    log_transaction("🛑 Program dihentikan oleh pengguna.")
    print("\n🛑 Program dihentikan oleh pengguna.")
    pi.stop()
except Exception as e:
    log_transaction(f"❌ ERROR: Terjadi kesalahan utama pada program: {str(e)}")