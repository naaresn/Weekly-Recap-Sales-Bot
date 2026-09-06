import base64
import io
import json
import gspread
import logging
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
import os
import telebot
import pandas as pd
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
from gspread_dataframe import set_with_dataframe


load_dotenv()

logging.basicConfig(level = logging.INFO,
                    format = "%(asctime)s [%(levelname)s] %(message)s",)

logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("API_TELEGRAM_BOT")
SPREADSHEET_NAME = os.environ.get("SPREADSHEET_NAME", "Data Penjualan")
OWNER_CHAT_ID = os.environ.get("OWNER_CHAT_ID")
STOK_MINIMAL = int(os.environ.get("STOK_MINIMAL", "5"))

if not BOT_TOKEN:
    raise RuntimeError("API_TELEGRAM_BOT belum diset di environtment variable")

bot = telebot.TeleBot(BOT_TOKEN)


def get_gspread_client():
    creds_b64 = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON_BASE64")
    creds_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")

    if creds_b64:
        creds_dict = json.loads(base64.b64decode(creds_b64).decode("utf-8"))
        return gspread.service_account_from_dict(creds_dict)

    if creds_json:
        creds_dict = json.loads(creds_json)
        return gspread.service_account_from_dict(creds_dict)

    logger.warning(
        "GOOGLE_SERVICE_ACCOUNT_JSON(_BASE64) tidak diset, fallback ke OAuth interaktif "
        "(hanya untuk development lokal, tidak akan jalan di server/container)."
    )
    return gspread.oauth()


def get_spreadsheet():
    gc = get_gspread_client()
    return gc.open(SPREADSHEET_NAME)

def get_sheet(sheet_name = "Transaksi"):
    spreadsheet = get_spreadsheet()
    return spreadsheet.worksheet(sheet_name)

def ensure_header(sheet):
    values = sheet.get_all_values()
    header = ["Tanggal", "Produk", "Qty", "Harga Satuan", "Total", "Stok Sisa"]

    if not values:
        sheet.append_row(header)
    elif values[0] != header:
        logger.warning("Header sheet tidak sesuai format: %s", values[0])
        
def get_data(): 
    sheet = get_sheet("Transaksi")
    data = sheet.get_all_records()   
    
    if not data:
        return pd.DataFrame(columns=["Tanggal", "Produk", "Qty", "Harga Satuan", "Total", "Stok Sisa"])
    
    return pd.DataFrame(data)

def update_stock(produk, qty_terjual):
    worksheet = get_sheet("Master Produk")
    records = worksheet.get_all_records()

    row_index = next((i for i, item in enumerate(records) if str(item.get("Produk")).strip().lower() == produk.lower()), None)

    if row_index is None:
        logger.warning("Produk '%s' tidak ditemukan di Master Produk, stok tidak diupdate", produk)
        return None
 
    row_number = row_index + 2
    stok_sekarang = records[row_index].get("Stok Sisa", 0)
 
    try:
        stok_sekarang = int(stok_sekarang)
    except (ValueError, TypeError):
        stok_sekarang = 0
 
    stok_baru = stok_sekarang - qty_terjual
    worksheet.update_cell(row_number, 4, stok_baru) 
 
    logger.info("Stok '%s' diupdate: %s -> %s", produk, stok_sekarang, stok_baru)
    return stok_baru

@bot.message_handler(commands=["jual"])
def handler_jual(message):
    try:
        parts = message.text.split()[1:]
        
        if len(parts) < 2:
            bot.reply_to(message, "Format salah. \n Contoh format: /jual ayam_goreng 5 15000")
            return
        
        produk = parts[0].replace("_", " ")
        qty = int(parts[1])
        
        
        if qty <= 0:
            bot.reply_to(message, "Qty tidak boleh 0")
            return
        
        sheet_transaksi = get_sheet("Transaksi")
        ensure_header(sheet_transaksi)
        
        try:
            sheet_master_produk = get_sheet("Master Produk")
            master_records_produk = sheet_master_produk.get_all_records()
            check_produk = next((item for item in master_records_produk if str(item.get("Produk")).strip().lower() == produk.lower()), None)
            
            if check_produk is not None:
                stok_tersedia = int(check_produk.get("Stok Sisa", 0) or 0)
                if qty > stok_tersedia:
                    bot.reply_to(message, f"Stok {produk} tersisa {stok_tersedia}, tidak bisa menjual {qty}")
                    return
        except Exception as e:
            logger.warning("Gagal cek stok sebelum transaksi: %s", e)
        
        if len(parts) >= 3:
            harga = float(parts[2])
        else:
            try:
                sheet_master = get_sheet("Master Produk")
                master_records = sheet_master.get_all_records()
                
                produk_data = next((item for item in master_records if str(item.get("Produk")).strip().lower() == produk.lower()), None)
                
                if produk_data and "Harga Satuan" in produk_data:
                    harga = float(produk_data["Harga Satuan"])
                else:
                    bot.reply_to(message, f"Produk '{produk}' tidak ditemukan dalam file. Tulis harga secara manual: '/jual {parts[0]} {qty} [harga]'")
                    return 
            except Exception as e:
                logger.warning("Gagal mengambil harga dari sheet: %s", e)
                bot.reply_to(message, "Gagal ambil harga. Sertakan harga saat mencatat transaksi")
                return
            
        total = qty * harga
        tanggal = datetime.now().strftime("%Y-%m-%d %H:%M")
                                 
        next_row = len(sheet_transaksi.get_all_values()) + 1
        formula_stok_sisa = f"=VLOOKUP(B{next_row}, 'Master Produk'!A:D, 4, FALSE)"
             
        sheet_transaksi.append_row([tanggal, produk, qty, harga, total, formula_stok_sisa], value_input_option="USER_ENTERED")   
        
        stok_baru = update_stock(produk, qty)
 
        balasan = f"Terjual: {produk} x{qty} @Rp{harga:,.0f} = Rp{total:,.0f}"
        if stok_baru is not None:
            balasan += f"\nSisa stok: {stok_baru}"
            if stok_baru <= STOK_MINIMAL:
                balasan += f"\n\u26a0\ufe0f Stok menipis! (<= {STOK_MINIMAL})"
        else:
            balasan += "\n(Stok tidak diupdate -- produk tidak ada di Master Produk)"
 
        bot.reply_to(message, balasan)
        logger.info("Transaksi baru: %s x%s oleh chat_id=%s", produk, qty, message.chat.id)
    
    except ValueError:
        bot.reply_to(message, "Qty dan harga harus berupa angka. Contoh: /jual baju_hitam 2 30000")
    except Exception as e:
        logger.exception("Gagal mencatat transaksi")
        bot.reply_to(message, f"Gagal mencatat transaksi: {e}")
        
@bot.message_handler(commands=["start", "help"])
def handler_start(message):
    bot.reply_to(message, """Halo, selamat datang di Bot Rekap penjualan \nCara mencatat transaksi: \n/jual [nama_produk] [qty] [harga_satuan] \ncontoh: /jual ayam_goreng 3 15000 \n\nRekap mingguan akan dikirim setiap jam 8 pagi \nIngin cek rekap sekarang? ketik /rekap""")
    

@bot.message_handler(commands=["rekap"])
def handler_rekap(message):
    try:
        df = get_data()
        recap = compute_recap(df)
        teks_recap = text_recap(recap)
        bot.reply_to(message, teks_recap)
    except Exception as e:
        logger.exception("Gagal rekap manual")
        bot.reply_to(message, f"Gagal rekap: {e}")
        
    
        
def compute_recap(df: pd.DataFrame) -> dict:
    df = df.copy()
        
    df["Qty"] = pd.to_numeric(df["Qty"], errors = "coerce").fillna(0)
    df["Total"] = pd.to_numeric(df["Total"], errors = "coerce").fillna(0)
    
    total_omset = df["Total"].sum()
    total_item_terjual = df["Qty"].sum()
    jumlah_transaksi = len(df)
    produk_terlaris = (df.groupby("Produk")["Qty"].sum().sort_values(ascending=False)).head(5)

    stok_menipis = pd.Series(dtype = float)
    try:
        sheet_master = get_sheet("Master Produk")
        df_master = pd.DataFrame(sheet_master.get_all_records())
        if not df_master.empty and "Stok Sisa" in df_master.columns and "Produk" in df_master.columns:
            df_master["Stok Sisa"] = pd.to_numeric(df_master["Stok Sisa"], errors="coerce").fillna(0)
            stok_terakhir = df_master.set_index("Produk")["Stok Sisa"]
            stok_menipis = stok_terakhir[stok_terakhir <= STOK_MINIMAL]
    except Exception:
        if "Stok Sisa" in df.columns:
            df_stock = df[df["Stok Sisa"] != ""].copy()
            if not df_stock.empty:
                df_stock["Stok Sisa"] = pd.to_numeric(df_stock["Stok Sisa"], errors="coerce")
                stok_terakhir = df_stock.groupby("Produk")["Stok Sisa"].last()
                stok_menipis = stok_terakhir[stok_terakhir <= STOK_MINIMAL]
    
    return {
        "total_omset": total_omset,
        "total_item_terjual": total_item_terjual,
        "jumlah_transaksi": jumlah_transaksi,
        "produk_terlaris": produk_terlaris,
        "stok_menipis": stok_menipis,
    }
    

def text_recap(rekap: dict) -> str:
    text = [
        "Rekap Penjualan",
        f"Total omset: Rp{rekap['total_omset']:,.0f}",
        f"Total item terjual: {int(rekap['total_item_terjual'])}",
        f"Jumlah transaksi: {rekap['jumlah_transaksi']}",
        "",
        "Produk Terlaris:",]
    
    for produk, qty in rekap["produk_terlaris"].items():
        text.append(f"-{produk}: {int(qty)} pcs")
    
    if not rekap["stok_menipis"].empty:
        text.append("")
        text.append(f"Stok menipis (<= {STOK_MINIMAL}):")
        for produk, stok in rekap["stok_menipis"].items():
            text.append(f"- {produk}: sisa {int(stok)}")
 
    return "\n".join(text)
    
def get_sheet_recap():
    return get_sheet("Rekap")    

def recap_to_sheet(rekap: dict):
    worksheet = get_sheet_recap()
    
    worksheet.update(range_name="B3", values=[[float(rekap["total_omset"])]])
    worksheet.update(range_name="B4", values=[[int(rekap["total_item_terjual"])]])
    worksheet.update(range_name="B5", values=[[int(rekap["jumlah_transaksi"])]])
    
    produk_terlaris = rekap["produk_terlaris"]
    rows = [[produk, int(qty)] for produk, qty in produk_terlaris.items()]
 
    while len(rows) < 5:
        rows.append(["", ""])
 
    worksheet.update(range_name="A9:B13", values=rows)
      
def rekap_mingguan():
    if not OWNER_CHAT_ID:
        logger.warning("OWNER_CHAT_ID belum di-set, skip rekap otomatis")
        return
    
    df = None
    rekap = None
    try:
        df = get_data()
        rekap = compute_recap(df)
        teks_rekap = text_recap(rekap)
        bot.send_message(OWNER_CHAT_ID, f"Rekap mingguan\n\n{teks_rekap}")
        recap_to_sheet(rekap)
        logger.info("Rekap mingguan terkirim ke chat_id=%s", OWNER_CHAT_ID)
    except Exception:
        logger.exception("Gagal mengirim rekap mingguan")

    # Pembuatan & pengiriman Excel dibungkus try/except terpisah
    if df is not None and rekap is not None:
        try:
            excel_buffer = io.BytesIO()
            with pd.ExcelWriter(excel_buffer, engine="openpyxl") as writer:
                # Sheet "Transaksi": seluruh data mentah dari get_data()
                df.to_excel(writer, sheet_name="Transaksi", index=False)
                
                # Sheet "Ringkasan": total omset, total item terjual, jumlah transaksi
                df_ringkasan = pd.DataFrame([
                    {"Metrik": "Total Omset", "Nilai": rekap["total_omset"]},
                    {"Metrik": "Total Item Terjual", "Nilai": rekap["total_item_terjual"]},
                    {"Metrik": "Jumlah Transaksi", "Nilai": rekap["jumlah_transaksi"]}
                ])
                df_ringkasan.to_excel(writer, sheet_name="Ringkasan", index=False)
                
                # Sheet "Produk Terlaris": top produk dari rekap["produk_terlaris"]
                df_terlaris = pd.DataFrame({
                    "Produk": rekap["produk_terlaris"].index,
                    "Jumlah Terjual": rekap["produk_terlaris"].values
                })
                df_terlaris.to_excel(writer, sheet_name="Produk Terlaris", index=False)
                
                # Sheet "Stok Menipis": hanya dibuat kalau rekap["stok_menipis"] tidak kosong
                if not rekap["stok_menipis"].empty:
                    df_stok = pd.DataFrame({
                        "Produk": rekap["stok_menipis"].index,
                        "Sisa Stok": rekap["stok_menipis"].values
                    })
                    df_stok.to_excel(writer, sheet_name="Stok Menipis", index=False)
            
            excel_buffer.seek(0)
            nama_file = f"rekap_penjualan_{datetime.now().strftime('%Y%m%d')}.xlsx"
            bot.send_document(OWNER_CHAT_ID, (nama_file, excel_buffer))
            logger.info("Excel rekap mingguan terkirim ke chat_id=%s", OWNER_CHAT_ID)
        except Exception:
            logger.exception("Gagal membuat atau mengirim file Excel rekap mingguan")


scheduler = BackgroundScheduler()
scheduler.add_job(rekap_mingguan, "cron", day_of_week = "mon", hour = 13, minute = 52)


class _HealthCheckHandler(BaseHTTPRequestHandler):
    """Handler minimal untuk healthcheck. Tidak melayani trafik bisnis apa pun."""

    def do_GET(self):
        if self.path in ("/", "/health"):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"OK")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        # Bisukan default access log supaya tidak membanjiri log bot
        pass


def start_health_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), _HealthCheckHandler)
    logger.info("Health check server listening on port %s", port)
    server.serve_forever()


if __name__ == "__main__":
    threading.Thread(target=start_health_server, daemon=True).start()
    scheduler.start()
    logger.info("Bot jalan")
    bot.infinity_polling()