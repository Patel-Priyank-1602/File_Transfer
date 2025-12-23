from flask import Flask, request, send_from_directory, render_template, redirect, url_for, session, Response, jsonify
import os
import qrcode
import io
import base64
from dotenv import load_dotenv
from werkzeug.utils import secure_filename
import mimetypes
import hashlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor
import logging
from datetime import datetime
import json

# Load environment variables
load_dotenv()

# Configuration
HOTSPOT_SSID = os.getenv("HOTSPOT_SSID")
HOTSPOT_PASSWORD = os.getenv("HOTSPOT_PASSWORD")
HOTSPOT_IP = os.getenv("HOTSPOT_IP")
PORT = int(os.getenv("PORT", 8000))

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY")

# Performance Configuration
CHUNK_SIZE = 2 * 1024 * 1024  # 2MB chunks
MAX_CONTENT_LENGTH = 2000 * 1024 * 1024  # 2GB max
app.config['MAX_CONTENT_LENGTH'] = MAX_CONTENT_LENGTH

NUM_PARALLEL_STREAMS = 4
STREAM_CHUNK_SIZE = 512 * 1024  # 512KB

# Upload Folder
UPLOAD_FOLDER = os.getenv("UPLOAD_FOLDER", "shared_files")
TEMP_FOLDER = os.path.join(UPLOAD_FOLDER, ".temp")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(TEMP_FOLDER, exist_ok=True)

# Thread pool and tracking
executor = ThreadPoolExecutor(max_workers=NUM_PARALLEL_STREAMS * 4)
active_transfers = {}
transfer_lock = threading.Lock()

# Chat Configuration
chat_messages = []
chat_lock = threading.Lock()
MAX_MESSAGES = 100
connected_users = {}
user_lock = threading.Lock()

# Helper Functions
def create_qr_data_uri(data):
    qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_L, box_size=10, border=4)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    byte_data = buf.getvalue()
    
    base64_data = base64.b64encode(byte_data).decode('utf-8')
    return f"data:image/png;base64,{base64_data}"

def get_file_hash(filename):
    return hashlib.md5(filename.encode()).hexdigest()

def get_username():
    """Get username from session or generate anonymous name"""
    if 'username' not in session:
        if 'logged_in' in session:
            session['username'] = 'Admin'
        else:
            # Generate anonymous username
            user_id = hashlib.md5(str(time.time()).encode()).hexdigest()[:6]
            session['username'] = f'User_{user_id}'
    return session['username']

def add_chat_message(username, message, message_type='text'):
    """Add a message to the chat"""
    with chat_lock:
        chat_messages.append({
            'username': username,
            'message': message,
            'timestamp': datetime.now().strftime('%H:%M:%S'),
            'type': message_type
        })
        # Keep only last MAX_MESSAGES
        if len(chat_messages) > MAX_MESSAGES:
            chat_messages.pop(0)

# Routes
@app.route("/", methods=["GET", "POST"])
def login():
    if "logged_in" in session:
        return redirect(url_for("dashboard"))
    error = None
    if request.method == "POST":
        if request.form["username"] == ADMIN_USERNAME and request.form["password"] == ADMIN_PASSWORD:
            session["logged_in"] = True
            session["username"] = "Admin"
            return redirect(url_for("dashboard"))
        else:
            error = "Invalid username or password"
    return render_template("login.html", error=error)

@app.route("/dashboard")
def dashboard():
    if "logged_in" not in session:
        return redirect(url_for("login"))

    wifi_string = f"WIFI:T:WPA;S:{HOTSPOT_SSID};P:{HOTSPOT_PASSWORD};;"
    wifi_qr_uri = create_qr_data_uri(wifi_string)

    url_string = f"http://{HOTSPOT_IP}:{PORT}/files"
    url_qr_uri = create_qr_data_uri(url_string)
    
    file_list = [f for f in os.listdir(UPLOAD_FOLDER) if not f.startswith('.')]

    # Get username
    username = get_username()

    return render_template(
        "dashboard.html",
        wifi_qr=wifi_qr_uri,
        url_qr=url_qr_uri,
        url_string_for_copy=url_string,
        files=file_list,
        username=username
    )

@app.route("/files", methods=["GET"])
def files():
    file_list = [f for f in os.listdir(UPLOAD_FOLDER) if not f.startswith('.')]
    username = get_username()
    return render_template("files.html", files=file_list, username=username)

@app.route("/upload_chunk", methods=["POST"])
def upload_chunk():
    try:
        chunk = request.files['chunk']
        chunk_index = int(request.form['chunkIndex'])
        total_chunks = int(request.form['totalChunks'])
        filename = secure_filename(request.form['filename'])
        stream_id = request.form.get('streamId', '0')
        
        transfer_id = get_file_hash(filename)
        
        # Store chunk
        temp_chunk_path = os.path.join(TEMP_FOLDER, f"{transfer_id}_chunk_{chunk_index}")
        chunk.save(temp_chunk_path)
        
        # Track progress
        with transfer_lock:
            if transfer_id not in active_transfers:
                active_transfers[transfer_id] = {
                    'filename': filename,
                    'total_chunks': total_chunks,
                    'received_chunks': set(),
                    'start_time': time.time(),
                    'total_bytes': 0
                }
            
            active_transfers[transfer_id]['received_chunks'].add(chunk_index)
            active_transfers[transfer_id]['total_bytes'] += os.path.getsize(temp_chunk_path)
            received = len(active_transfers[transfer_id]['received_chunks'])
            
            # Check if complete
            if received == total_chunks:
                final_path = os.path.join(UPLOAD_FOLDER, filename)
                with open(final_path, 'wb') as outfile:
                    for i in range(total_chunks):
                        chunk_path = os.path.join(TEMP_FOLDER, f"{transfer_id}_chunk_{i}")
                        if os.path.exists(chunk_path):
                            with open(chunk_path, 'rb') as infile:
                                outfile.write(infile.read())
                            os.remove(chunk_path)
                
                elapsed = time.time() - active_transfers[transfer_id]['start_time']
                file_size = os.path.getsize(final_path)
                speed_mbps = (file_size / elapsed) / (1024 * 1024)
                
                print(f"✓ File assembled: {filename} ({file_size / (1024*1024):.2f} MB in {elapsed:.2f}s = {speed_mbps:.2f} MB/s)")
                
                # Add system message to chat
                username = get_username()
                add_chat_message('System', f'{username} uploaded {filename}', 'system')
                
                del active_transfers[transfer_id]
                
                return jsonify({'success': True, 'completed': True, 'speed': speed_mbps})
        
        return jsonify({'success': True, 'completed': False, 'received': received, 'total': total_chunks})
        
    except Exception as e:
        print(f"Chunk upload error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route("/upload_progress")
def upload_progress():
    """SSE endpoint for real-time progress updates on controller"""
    def generate():
        while True:
            with transfer_lock:
                if active_transfers:
                    data = []
                    for transfer_id, info in active_transfers.items():
                        received = len(info['received_chunks'])
                        total = info['total_chunks']
                        percent = (received / total) * 100 if total > 0 else 0
                        elapsed = time.time() - info['start_time']
                        speed = (info['total_bytes'] / elapsed) / (1024 * 1024) if elapsed > 0 else 0
                        
                        data.append({
                            'filename': info['filename'],
                            'percent': round(percent, 1),
                            'speed': round(speed, 2),
                            'received': received,
                            'total': total
                        })
                    
                    yield f"data: {jsonify(data).get_data(as_text=True)}\n\n"
                else:
                    yield f"data: []\n\n"
            
            time.sleep(0.5)
    
    return Response(generate(), mimetype='text/event-stream')

@app.route("/upload_parallel", methods=["POST"])
def upload_parallel():
    try:
        f = request.files["file"]
        if f.filename:
            filename = secure_filename(f.filename)
            filepath = os.path.join(UPLOAD_FOLDER, filename)
            f.save(filepath)
            print(f"File uploaded (fallback): {filename}")
            
            # Add system message to chat
            username = get_username()
            add_chat_message('System', f'{username} uploaded {filename}', 'system')
    except Exception as e:
        print(f"Upload error: {e}")
    
    if request.referrer and 'dashboard' in request.referrer:
        return redirect(url_for("dashboard"))
    else:
        return redirect(url_for("files"))

@app.route("/download_parallel/<filename>")
def download_parallel(filename):
    filepath = os.path.join(UPLOAD_FOLDER, secure_filename(filename))
    
    if not os.path.exists(filepath):
        return "File not found", 404
    
    file_size = os.path.getsize(filepath)
    range_header = request.headers.get('Range', None)
    
    if range_header:
        byte_range = range_header.replace('bytes=', '').split('-')
        start = int(byte_range[0]) if byte_range[0] else 0
        end = int(byte_range[1]) if byte_range[1] else file_size - 1
        
        length = end - start + 1
        
        def generate_range():
            with open(filepath, 'rb') as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk_size = min(CHUNK_SIZE, remaining)
                    data = f.read(chunk_size)
                    if not data:
                        break
                    remaining -= len(data)
                    yield data
        
        response = Response(generate_range(), 206, mimetype=mimetypes.guess_type(filename)[0])
        response.headers['Content-Range'] = f'bytes {start}-{end}/{file_size}'
        response.headers['Content-Length'] = str(length)
        response.headers['Accept-Ranges'] = 'bytes'
        response.headers['Cache-Control'] = 'no-cache'
        
        return response
    else:
        def generate():
            with open(filepath, 'rb') as f:
                while True:
                    chunk = f.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    yield chunk
        
        mimetype = mimetypes.guess_type(filename)[0] or 'application/octet-stream'
        response = Response(generate(), mimetype=mimetype)
        response.headers['Content-Disposition'] = f'attachment; filename="{filename}"'
        response.headers['Content-Length'] = str(file_size)
        response.headers['Accept-Ranges'] = 'bytes'
        response.headers['Cache-Control'] = 'no-cache'
        
        return response

# Chat Routes
@app.route("/chat/send", methods=["POST"])
def send_message():
    """Send a chat message"""
    try:
        data = request.json
        message = data.get('message', '').strip()
        
        if not message:
            return jsonify({'success': False, 'error': 'Empty message'}), 400
        
        username = get_username()
        add_chat_message(username, message)
        
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route("/chat/messages")
def get_messages():
    """SSE endpoint for real-time chat messages"""
    def generate():
        last_count = 0
        while True:
            with chat_lock:
                current_count = len(chat_messages)
                if current_count > last_count:
                    # Send only new messages
                    new_messages = chat_messages[last_count:]
                    yield f"data: {json.dumps(new_messages)}\n\n"
                    last_count = current_count
                elif current_count < last_count:
                    # Messages were cleared, send all
                    yield f"data: {json.dumps(chat_messages)}\n\n"
                    last_count = current_count
            
            time.sleep(0.3)
    
    return Response(generate(), mimetype='text/event-stream')

@app.route("/chat/history")
def chat_history():
    """Get all chat history"""
    with chat_lock:
        return jsonify(chat_messages)

@app.route("/chat/set_username", methods=["POST"])
def set_username():
    """Allow users to set custom username"""
    try:
        data = request.json
        new_username = data.get('username', '').strip()
        
        if not new_username or len(new_username) > 20:
            return jsonify({'success': False, 'error': 'Invalid username'}), 400
        
        old_username = session.get('username', None)
        session['username'] = new_username
        
        # Only add system message if username is being changed (not first time)
        if old_username and old_username != new_username:
            add_chat_message('System', f'{old_username} changed name to {new_username}', 'system')
        elif not old_username:
            # First time setting username - announce join
            add_chat_message('System', f'{new_username} joined the chat', 'system')
        
        return jsonify({'success': True, 'username': new_username})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route("/chat")
def chat_page():
    """Standalone chat page"""
    username = get_username()
    return render_template("chat_app.html", username=username)

@app.route("/logout")
def logout():
    username = session.get('username', 'User')
    add_chat_message('System', f'{username} left the chat', 'system')
    session.pop("logged_in", None)
    session.pop("username", None)
    return redirect(url_for("login"))

log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

if __name__ == "__main__":
    if HOTSPOT_SSID == "Priyank_" or HOTSPOT_PASSWORD == "12345678":
        print("="*50)
        print("!!! WARNING: Please edit your .env file !!!")
        print("You are using the default HOTSPOT_SSID or HOTSPOT_PASSWORD.")
        print("="*50)
    
    if not all([HOTSPOT_SSID, HOTSPOT_PASSWORD, HOTSPOT_IP, PORT, ADMIN_USERNAME, ADMIN_PASSWORD, app.secret_key]):
        print("="*50)
        print("!!! ERROR: Missing configuration !!!")
        print("One or more variables are not set in your .env file.")
        print("Please check your .env file and ensure all variables are set.")
        print("="*50)
    else:
        print(f"")
        print(f"╔═════════════════════════════════════════════════════╗")
        print(f"║            🚀 TRANSFER FILE SERVER 🚀               ║")
        print(f"╚═════════════════════════════════════════════════════╝")
        print(f"")
        print(f"⚡ Performance Features:")
        print(f"   • {NUM_PARALLEL_STREAMS} parallel TCP streams")
        print(f"   • {CHUNK_SIZE / (1024*1024):.0f}MB chunks per stream")
        print(f"   • Range request support for parallel downloads")
        print(f"   • Real-time speed monitoring")
        print(f"   • Up to 2GB file support")
        print(f"   • 💬 Real-time chat between users")
        print(f"")
        print(f"📶 Network Configuration:")
        print(f"   • Hotspot SSID: {HOTSPOT_SSID}")
        print(f"   • Server IP: {HOTSPOT_IP}")
        print(f"   • Port: {PORT}")
        print(f"")
        print(f"🖥️  Access from THIS PC:")
        print(f"   http://127.0.0.1:{PORT}")
        print(f"")
        print(f"📱 Access from mobile devices:")
        print(f"   1. Connect to hotspot: {HOTSPOT_SSID}")
        print(f"   2. Open: http://{HOTSPOT_IP}:{PORT}/files")
        print(f"")
        
        app.run(host="0.0.0.0", port=PORT, threaded=True)