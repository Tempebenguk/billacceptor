import datetime
import time
import requests

# Konfigurasi API
INVOICE_API = "https://api.dev.xpdisi.id/invoice/device/bic01"

def fetch_invoice_data():
    """Mengambil data invoice dari API."""
    try:
        response = requests.get(INVOICE_API, timeout=5)
        if response.status_code == 200:
            return response.json()
        else:
            print(f"⚠ Gagal mengambil data invoice: {response.status_code}")
    except requests.exceptions.RequestException as e:
        print(f"⚠ Error saat request: {e}")
    return None

def get_valid_payment_token(data):
    """Mendapatkan PaymentToken terbaru yang usianya kurang dari 3 menit."""
    now = datetime.datetime.utcnow()
    
    for entry in data.get("data", []):
        created_at = datetime.datetime.fromisoformat(entry["CreatedAt"].replace("Z", "+00:00"))

        # Jika transaksi masih kurang dari 3 menit, simpan PaymentToken
        if (now - created_at).total_seconds() <= 180:
            return entry["PaymentToken"]

    return None

def main_loop():
    """Loop utama yang berjalan setiap 1 detik."""
    while True:
        json_response = fetch_invoice_data()
        
        if json_response:
            valid_token = get_valid_payment_token(json_response)
            if valid_token:
                print(f"✅ Payment Token valid ditemukan: {valid_token}")
            else:
                print("🚫 Tidak ada transaksi valid (<3 menit)")
        
        time.sleep(1)  # Tunggu 1 detik sebelum request ulang

if __name__ == "__main__":
    main_loop()
