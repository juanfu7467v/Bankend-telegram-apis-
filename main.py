import os
import re
import asyncio
import time
import json
import mimetypes
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from telethon import TelegramClient, events
from telethon.sessions import StringSession

# --- Configuración y Variables de Entorno ---
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
PUBLIC_URL = os.getenv("PUBLIC_URL", "https://bankend-tlgm-2p.fly.dev").rstrip("/")
SESSION_STRING = os.getenv("SESSION_STRING", None)
PORT = int(os.getenv("PORT", 8080))

DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

LEDERDATA_BOT_ID = "@LEDERDATA_OFC_BOT" 
LEDERDATA_BACKUP_BOT_ID = "@lederdata_publico_bot"

TIMEOUT_PRIMARY = 30  
TIMEOUT_BACKUP = 40   
BOT_BLOCK_HOURS = 3   

# Rastreador de fallos para bloqueo de 3 horas
bot_fail_tracker = {}

# --- Funciones de Utilidad ---
def is_bot_blocked(bot_id: str) -> bool:
    last_fail_time = bot_fail_tracker.get(bot_id)
    if not last_fail_time: return False
    if datetime.now() < last_fail_time + timedelta(hours=BOT_BLOCK_HOURS):
        return True
    return False

def record_bot_failure(bot_id: str):
    bot_fail_tracker[bot_id] = datetime.now()

def analyze_content(text: str):
    """Analiza el contenido de los mensajes para determinar el estado de la respuesta."""
    # Patrón de "No encontrado" solicitado
    not_found_pattern = r"\[⚠️\]\s*(no se encontro información|no se han encontrado resultados|no se encontró una|no hay resultados|no tenemos datos|no se encontraron registros)"
    
    if re.search(not_found_pattern, text, re.IGNORECASE | re.DOTALL):
        return "NOT_FOUND"
    
    if "⛔ ANTI-SPAM" in text.upper() or "ANTI-SPAM" in text.upper():
        return "ANTI_SPAM"
        
    return "SUCCESS"

def clean_text(raw_text: str):
    """Limpieza estándar de publicidad/headers manteniendo la lógica original."""
    if not raw_text: return ""
    text = raw_text
    text = re.sub(r"\[#?LEDER_BOT\]|\[CONSULTA PE\]", "", text, flags=re.IGNORECASE)
    header_pattern = r"^\[.*?\]\s*→\s*.*?\[.*?\](\r?\n){1,2}"
    text = re.sub(header_pattern, "", text, flags=re.IGNORECASE | re.DOTALL)
    footer_pattern = r"((\r?\n){1,2}\[|Página\s*\d+\/\d+.*|Credits\s*:.+|\s*@lederdata.*|Créditos\s*:\s*\d+)"
    text = re.sub(footer_pattern, "", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"\-{3,}", "", text).strip()
    return text

# --- Lógica de Interacción con Telegram ---
async def query_bot(client, bot_id, command, timeout):
    """Envía un comando a un bot específico y espera la respuesta consolidada."""
    all_messages = []
    last_msg_time = [time.time()]
    
    @client.on(events.NewMessage(incoming=True, from_users=bot_id))
    async def handler(event):
        last_msg_time[0] = time.time()
        all_messages.append(event.message)

    try:
        await client.send_message(bot_id, command)
        start_time = time.time()
        
        # Espera de hasta 'timeout' segundos
        while True:
            elapsed = time.time() - start_time
            silence = time.time() - last_msg_time[0]
            
            # Si ya hay mensajes y hay silencio de 4 segundos, asumimos que terminó
            if len(all_messages) > 0 and silence > 4.0:
                break
            # Si excedemos el timeout total
            if elapsed > timeout:
                break
            await asyncio.sleep(0.5)
            
        return all_messages
    finally:
        client.remove_event_handler(handler)

async def send_telegram_command(command: str):
    client = None
    try:
        if not SESSION_STRING: raise Exception("SESSION_STRING no configurada")
        
        client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
        await client.connect()

        final_response_messages = []
        use_backup = False

        # 1. INTENTO CON BOT PRINCIPAL
        if not is_bot_blocked(LEDERDATA_BOT_ID):
            print(f"--- Consultando Bot Principal: {LEDERDATA_BOT_ID} ---")
            primary_msgs = await query_bot(client, LEDERDATA_BOT_ID, command, TIMEOUT_PRIMARY)
            
            if not primary_msgs:
                print("Bot principal no respondió. Bloqueando por 3 horas.")
                record_bot_failure(LEDERDATA_BOT_ID)
                use_backup = True
            else:
                full_text = "\n".join([m.text for m in primary_msgs if m.text])
                status = analyze_content(full_text)
                
                if status == "NOT_FOUND":
                    return {"status": "error", "message": "No se encontraron resultados."}
                elif status == "ANTI_SPAM":
                    print("Anti-spam detectado en principal. Pasando a respaldo.")
                    use_backup = True
                else:
                    final_response_messages = primary_msgs
        else:
            print("Bot principal bloqueado temporalmente. Usando respaldo directamente.")
            use_backup = True

        # 2. INTENTO CON BOT DE RESPALDO (SI CORRESPONDE)
        if use_backup:
            print(f"--- Consultando Bot Respaldo: {LEDERDATA_BACKUP_BOT_ID} ---")
            backup_msgs = await query_bot(client, LEDERDATA_BACKUP_BOT_ID, command, TIMEOUT_BACKUP)
            
            if not backup_msgs:
                return {"status": "error", "message": "Ningún bot respondió a la consulta."}
            
            full_text_backup = "\n".join([m.text for m in backup_msgs if m.text])
            if analyze_content(full_text_backup) == "NOT_FOUND":
                return {"status": "error", "message": "No se encontraron resultados."}
            
            final_response_messages = backup_msgs

        # 3. PROCESAMIENTO FINAL DE RESULTADOS
        consolidated_text = ""
        file_urls = []

        for msg in final_response_messages:
            # Extraer texto limpio
            if msg.text:
                consolidated_text += clean_text(msg.text) + "\n"
            
            # Procesar archivos adjuntos
            if msg.media:
                ext = mimetypes.guess_extension(msg.file.mime_type) if hasattr(msg.file, 'mime_type') else '.jpg'
                fname = f"file_{msg.id}{ext or '.dat'}"
                path = await client.download_media(msg, file=os.path.join(DOWNLOAD_DIR, fname))
                if path:
                    file_urls.append({"url": f"{PUBLIC_URL}/files/{os.path.basename(path)}"})

        return {
            "status": "success",
            "data": consolidated_text.strip(),
            "files": file_urls
        }

    except Exception as e:
        return {"status": "error", "message": str(e)}
    finally:
        if client: await client.disconnect()

# --- Flask App ---
app = Flask(__name__)
CORS(app)

def run_cmd(cmd):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try: return loop.run_until_complete(send_telegram_command(cmd))
    finally: loop.close()

@app.route("/files/<path:filename>")
def get_file(filename):
    return send_from_directory(DOWNLOAD_DIR, filename)

# --- ENDPOINTS (Mantenidos intactos) ---

@app.route("/cla", methods=["GET"])
def cla():
    dni = request.args.get("dni")
    if not dni or len(dni) != 8: return jsonify({"error": "DNI inválido"}), 400
    return jsonify(run_cmd(f"/cla {dni}"))

@app.route("/afp", methods=["GET"])
def afp():
    dni = request.args.get("dni")
    if not dni or len(dni) != 8: return jsonify({"error": "DNI inválido"}), 400
    return jsonify(run_cmd(f"/afp {dni}"))

@app.route("/bdir", methods=["GET"])
def bdir():
    dir_query = request.args.get("direccion")
    if not dir_query or len(dir_query) < 9: return jsonify({"error": "Dirección muy corta"}), 400
    return jsonify(run_cmd(f"/bdir {dir_query}"))

@app.route("/pasaporte", methods=["GET"])
def pasaporte():
    pass_num = request.args.get("pasaporte")
    if not pass_num or len(pass_num) < 5: return jsonify({"error": "Pasaporte inválido"}), 400
    return jsonify(run_cmd(f"/pasaporte {pass_num}"))

@app.route("/cedula", methods=["GET"])
def cedula():
    ce = request.args.get("cedula")
    if not ce or len(ce) < 7: return jsonify({"error": "Cédula inválida"}), 400
    return jsonify(run_cmd(f"/cedula {ce}"))

@app.route("/dend", methods=["GET"])
def dend():
    dni = request.args.get("dni")
    if not dni or len(dni) != 8: return jsonify({"error": "DNI inválido"}), 400
    return jsonify(run_cmd(f"/dend {dni}"))

@app.route("/dence", methods=["GET"])
def dence():
    ce = request.args.get("ce")
    if not ce or not (6 <= len(ce) <= 12): return jsonify({"error": "CE inválido"}), 400
    return jsonify(run_cmd(f"/dence {ce}"))

@app.route("/denpas", methods=["GET"])
def denpas():
    pass_num = request.args.get("pasaporte")
    if not pass_num or not (6 <= len(pass_num) <= 12): return jsonify({"error": "Pasaporte inválido"}), 400
    return jsonify(run_cmd(f"/denpas {pass_num}"))

@app.route("/denci", methods=["GET"])
def denci():
    ci = request.args.get("ci")
    if not ci or not (6 <= len(ci) <= 12): return jsonify({"error": "CI inválida"}), 400
    return jsonify(run_cmd(f"/denci {ci}"))

@app.route("/denp", methods=["GET"])
def denp():
    placa = request.args.get("placa")
    if not placa or not (5 <= len(placa) <= 7): return jsonify({"error": "Placa inválida"}), 400
    return jsonify(run_cmd(f"/denp {placa}"))

@app.route("/denar", methods=["GET"])
def denar():
    serie = request.args.get("serie")
    if not serie or not (5 <= len(serie) <= 13): return jsonify({"error": "Serie inválida"}), 400
    return jsonify(run_cmd(f"/denar {serie}"))

@app.route("/dencl", methods=["GET"])
def dencl():
    clave = request.args.get("clave")
    if not clave or not (5 <= len(clave) <= 11): return jsonify({"error": "Clave inválida"}), 400
    return jsonify(run_cmd(f"/dencl {clave}"))

@app.route("/cafp", methods=["GET"])
def cafp():
    dni = request.args.get("dni")
    if not dni or len(dni) != 8: return jsonify({"error": "DNI inválido"}), 400
    return jsonify(run_cmd(f"/cafp {dni}"))

@app.route("/sbs", methods=["GET"])
def sbs():
    dni = request.args.get("dni")
    if not dni or len(dni) != 8: return jsonify({"error": "DNI inválido"}), 400
    return jsonify(run_cmd(f"/sbs {dni}"))

@app.route("/")
def root():
    return jsonify({"status": "API Active", "version": "6.1"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
