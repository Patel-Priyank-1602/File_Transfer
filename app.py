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
from functools import wraps
import signal
import atexit

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
CHUNK_SIZE = 2 * 1024 * 1024  # 2MB chunks for download
MAX_CONTENT_LENGTH = 10 * 1024 * 1024 * 1024  # 10GB max file size
app.config['MAX_CONTENT_LENGTH'] = MAX_CONTENT_LENGTH

NUM_PARALLEL_STREAMS = 4
STREAM_CHUNK_SIZE = 1 * 1024 * 1024  # 1MB chunks for upload (better for large files)

# Upload Folder
UPLOAD_FOLDER = os.getenv("UPLOAD_FOLDER", "shared_files")
TEMP_FOLDER = os.path.join(UPLOAD_FOLDER, ".temp")
METADATA_FOLDER = os.path.join(UPLOAD_FOLDER, ".metadata")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(TEMP_FOLDER, exist_ok=True)
os.makedirs(METADATA_FOLDER, exist_ok=True)

# Thread pool and tracking
executor = ThreadPoolExecutor(max_workers=NUM_PARALLEL_STREAMS * 4)
active_transfers = {}
transfer_lock = threading.Lock()

# Track files being assembled (prevent download during assembly)
assembling_files = set()
assembling_lock = threading.Lock()

# Chat Configuration
chat_messages = []
chat_lock = threading.Lock()
MAX_MESSAGES = 100
connected_users = {}  # {session_id: {'username': str, 'last_seen': timestamp, 'is_server': bool}}
kicked_users = set()
kicked_lock = threading.Lock()
user_lock = threading.Lock()
USER_TIMEOUT = 30  # seconds - consider user offline after this time

# File metadata tracking
file_metadata = {}
metadata_lock = threading.Lock()


def set_user_role(role):
    session['role'] = role

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
    """Get username from session - returns None if not set"""
    return session.get('username', None)

def is_username_set():
    """Check if username is set in session"""
    return 'username' in session and session['username'] is not None

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

def get_metadata_path(filename):
    """Get path to metadata file for a given filename"""
    return os.path.join(METADATA_FOLDER, f"{filename}.json")

def save_file_metadata(filename, metadata):
    """Save metadata for a file"""
    try:
        metadata_path = get_metadata_path(filename)
        with open(metadata_path, 'w') as f:
            json.dump(metadata, f, indent=2)
    except Exception as e:
        print(f"Error saving metadata for {filename}: {e}")

def load_file_metadata(filename):
    """Load metadata for a file"""
    try:
        metadata_path = get_metadata_path(filename)
        if os.path.exists(metadata_path):
            with open(metadata_path, 'r') as f:
                return json.load(f)
    except Exception as e:
        print(f"Error loading metadata for {filename}: {e}")
    
    # Return default metadata if file doesn't exist or error
    return {
        'filename': filename,
        'uploaded_by': 'Unknown',
        'upload_time': 'Unknown',
        'file_size': 0,
        'downloads': []
    }

def add_download_record(filename, username):
    """Add a download record to file metadata"""
    metadata = load_file_metadata(filename)
    
    download_record = {
        'username': username,
        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    }
    
    if 'downloads' not in metadata:
        metadata['downloads'] = []
    
    metadata['downloads'].append(download_record)
    save_file_metadata(filename, metadata)

def update_user_activity():
    """Update user's last seen timestamp"""
    if 'logged_in' in session:
        username = get_username()
        if username:
            # Create a unique identifier using remote address and username
            # This helps distinguish multiple users from same IP
            session_id = f"{request.remote_addr}_{username}_{session.get('_id', 'default')}"
            is_server = session.get('role') == 'server'
            with user_lock:
                connected_users[session_id] = {
                    'username': username,
                    'last_seen': time.time(),
                    'is_server': is_server
                }
                # Clean up old users
                current_time = time.time()
                to_remove = []
                for sid, user_data in connected_users.items():
                    if current_time - user_data['last_seen'] > USER_TIMEOUT:
                        to_remove.append(sid)
                for sid in to_remove:
                    del connected_users[sid]

def get_online_users():
    """Get list of currently online users"""
    update_user_activity()
    with user_lock:
        current_time = time.time()
        online_dict = {}  # Use dict to track unique users
        to_remove = []
        for sid, user_data in connected_users.items():
            if current_time - user_data['last_seen'] <= USER_TIMEOUT:
                username = user_data['username']
                # If user already exists, prefer server status if current is server
                if username not in online_dict or user_data['is_server']:
                    online_dict[username] = {
                        'username': username,
                        'is_server': user_data['is_server']
                    }
            else:
                to_remove.append(sid)
        # Clean up offline users
        for sid in to_remove:
            del connected_users[sid]
        return list(online_dict.values())

def save_all_metadata_to_file():
    """Save all file metadata and activity to a text file"""
    try:
        timestamp = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
        log_filename = f"transfer_log_{timestamp}.txt"
        
        with open(log_filename, 'w', encoding='utf-8') as f:
            f.write("=" * 80 + "\n")
            f.write("FILE TRANSFER SERVER - ACTIVITY LOG\n")
            f.write(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write("=" * 80 + "\n\n")
            
            # Get all files
            file_list = [f for f in os.listdir(UPLOAD_FOLDER) if not f.startswith('.')]
            
            if not file_list:
                f.write("No files were uploaded during this session.\n\n")
            else:
                f.write(f"TOTAL FILES: {len(file_list)}\n")
                f.write("-" * 80 + "\n\n")
                
                for filename in sorted(file_list):
                    metadata = load_file_metadata(filename)
                    filepath = os.path.join(UPLOAD_FOLDER, filename)
                    
                    f.write(f"FILE: {filename}\n")
                    f.write(f"  Uploaded by: {metadata.get('uploaded_by', 'Unknown')}\n")
                    f.write(f"  Upload time: {metadata.get('upload_time', 'Unknown')}\n")
                    
                    if os.path.exists(filepath):
                        file_size = os.path.getsize(filepath)
                        f.write(f"  File size: {file_size:,} bytes ({file_size / (1024*1024):.2f} MB)\n")
                    else:
                        f.write(f"  File size: File not found\n")
                    
                    downloads = metadata.get('downloads', [])
                    f.write(f"  Total downloads: {len(downloads)}\n")
                    
                    if downloads:
                        f.write(f"  Download history:\n")
                        for i, download in enumerate(downloads, 1):
                            f.write(f"    {i}. {download.get('username', 'Unknown')} - {download.get('timestamp', 'Unknown')}\n")
                    else:
                        f.write(f"  Download history: No downloads yet\n")
                    
                    f.write("\n" + "-" * 80 + "\n\n")
            
            # Add chat messages summary
            f.write("\n" + "=" * 80 + "\n")
            f.write("CHAT MESSAGES SUMMARY\n")
            f.write("=" * 80 + "\n\n")
            
            with chat_lock:
                if chat_messages:
                    f.write(f"TOTAL MESSAGES: {len(chat_messages)}\n")
                    f.write("-" * 80 + "\n\n")
                    for msg in chat_messages:
                        f.write(f"[{msg.get('timestamp', 'Unknown')}] {msg.get('username', 'Unknown')}: {msg.get('message', '')}\n")
                else:
                    f.write("No chat messages during this session.\n")
            
            f.write("\n" + "=" * 80 + "\n")
            f.write("END OF LOG\n")
            f.write("=" * 80 + "\n")
        
        print(f"\n✓ All activity saved to: {log_filename}")
        return log_filename
    except Exception as e:
        print(f"Error saving metadata to file: {e}")
        import traceback
        traceback.print_exc()
        return None

# Authentication decorator
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'logged_in' not in session:
            return redirect(url_for('login', next=request.url))
        return f(*args, **kwargs)
    return decorated_function

@app.before_request
def check_if_kicked():
    username = session.get('username')
    if not username:
        return

    with kicked_lock:
        if username in kicked_users:
            session.clear()
            return redirect(url_for("login"))

# Routes
@app.route("/", methods=["GET", "POST"])
def login():
    # ALWAYS show login page first - no auto-redirect
    # This ensures login page appears every time, whether old or new user
    error = None
    if request.method == "POST":
        if request.form["username"] == ADMIN_USERNAME and request.form["password"] == ADMIN_PASSWORD:
            # Get client name from form
            client_name = request.form.get("client_name", "").strip()
            if not client_name:
                error = "Please enter your display name"
            elif len(client_name) > 20:
                error = "Display name must be 20 characters or less"
            else:
                # Store session data
                session["logged_in"] = True
                session["admin_username"] = "Admin"  # Keep admin username separate
                
                # Store client name in session
                old_client_name = session.get("username", None)
                session["username"] = client_name
                
                # Set access token for /files route (for remote users)
                session["files_access_token"] = "granted"
                
                # Set session as permanent so it persists across page refreshes
                # This allows users to refresh without being redirected to login
                session.permanent = True
                
                # If this is a name change (not first login), add system message
                if old_client_name and old_client_name != client_name:
                    add_chat_message('System', f'{old_client_name} changed name to {client_name}', 'system')
                elif not old_client_name:
                    # First time login - announce join
                    add_chat_message('System', f'{client_name} joined the chat', 'system')
                
                # Redirect to the 'next' page if it exists
                next_page = request.args.get('next')
                if next_page:
                    return redirect(next_page)
                
                # Check if from localhost - redirect to dashboard, otherwise files
                is_localhost = (request.remote_addr in ['127.0.0.1', 'localhost', '::1'] or 
                                '127.0.0.1' in request.host or 
                                'localhost' in request.host or
                                request.environ.get('HTTP_HOST', '').startswith('127.0.0.1') or
                                request.environ.get('HTTP_HOST', '').startswith('localhost'))
                
                if is_localhost:
                    return redirect(url_for("dashboard"))
                else:
                    # Remote access - redirect to files page
                    return redirect(url_for("files"))
        else:
            error = "Invalid username or password"
    
    return render_template("login.html", error=error)

@app.route("/dashboard")
@login_required
def dashboard():
    set_user_role('server')
    # Ensure username is set - if not, redirect to login
    username = get_username()
    if not username or not is_username_set():
        session.clear()
        return redirect(url_for('login', next=request.url))
    
    # Track user activity
    update_user_activity()
    
    wifi_string = f"WIFI:T:WPA;S:{HOTSPOT_SSID};P:{HOTSPOT_PASSWORD};;"
    wifi_qr_uri = create_qr_data_uri(wifi_string)

    url_string = f"http://{HOTSPOT_IP}:{PORT}/files"
    url_qr_uri = create_qr_data_uri(url_string)
    
    file_list = [f for f in os.listdir(UPLOAD_FOLDER) if not f.startswith('.')]

    # Get username
    username = get_username()
    username_set = is_username_set()
    
    # Check which files are being assembled
    with assembling_lock:
        assembling_list = list(assembling_files)
    
    # Load metadata for all files
    files_metadata = {}
    for filename in file_list:
        files_metadata[filename] = load_file_metadata(filename)

    return render_template(
        "dashboard.html",
        wifi_qr=wifi_qr_uri,
        url_qr=url_qr_uri,
        url_string_for_copy=url_string,
        files=file_list,
        username=username or '',
        username_set=username_set,
        assembling_files=assembling_list,
        files_metadata=files_metadata
    )

@app.route("/files", methods=["GET"])
def files():
    set_user_role('client')
    # Check for valid session and username - allow refresh without redirecting to login
    if 'logged_in' not in session or not is_username_set():
        # No valid session - clear and redirect to login
        session.clear()
        return redirect(url_for('login', next=request.url))
    
    # Check for access token only on FIRST access (after login)
    # If token exists, set it as permanent so refresh works
    if 'files_access_token' in session and session.get('files_access_token') == 'granted':
        # Keep token in session for refresh capability
        # Mark session as permanent so it persists across refreshes
        session.permanent = True
    elif 'logged_in' in session and is_username_set():
        # User has valid session but no token - this means they're refreshing
        # Allow access if they have logged_in and username set
        pass
    else:
        # No valid session at all - redirect to login
        session.clear()
        return redirect(url_for('login', next=request.url))
    
    # Get username from session
    username = get_username()
    if not username:
        # No username - clear and redirect to login
        session.clear()
        return redirect(url_for('login', next=request.url))
    
    file_list = [f for f in os.listdir(UPLOAD_FOLDER) if not f.startswith('.')]
    username_set = is_username_set()
    
    # Check which files are being assembled
    with assembling_lock:
        assembling_list = list(assembling_files)
    
    # Load metadata for all files
    files_metadata = {}
    for filename in file_list:
        files_metadata[filename] = load_file_metadata(filename)
    
    # Keep the access token in session so refresh works
    # Don't remove it - this allows users to refresh without being redirected to login
    
    return render_template("files.html", files=file_list, username=username or '', username_set=username_set, assembling_files=assembling_list, files_metadata=files_metadata,)

@app.route("/check_username")
@login_required
def check_username():
    """Check if username is set"""
    return jsonify({'username_set': is_username_set(), 'username': get_username()})

@app.route("/upload_chunk", methods=["POST"])
@login_required
def upload_chunk():
    # Check if username is set
    if not is_username_set():
        return jsonify({'success': False, 'error': 'Please set your username first'}), 400
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
        
        # Verify chunk was saved
        if not os.path.exists(temp_chunk_path):
            print(f"ERROR: Chunk {chunk_index} not saved!")
            return jsonify({'success': False, 'error': 'Chunk save failed'}), 500
        
        chunk_size = os.path.getsize(temp_chunk_path)
        print(f"✓ Chunk {chunk_index}/{total_chunks} saved ({chunk_size} bytes)")
        
        # Track progress
        with transfer_lock:
            if transfer_id not in active_transfers:
                active_transfers[transfer_id] = {
                    'filename': filename,
                    'total_chunks': total_chunks,
                    'received_chunks': set(),
                    'start_time': time.time(),
                    'total_bytes': 0,
                    'chunk_sizes': {}
                }
            
            active_transfers[transfer_id]['received_chunks'].add(chunk_index)
            active_transfers[transfer_id]['chunk_sizes'][chunk_index] = chunk_size
            active_transfers[transfer_id]['total_bytes'] += chunk_size
            received = len(active_transfers[transfer_id]['received_chunks'])
            
            print(f"Progress: {received}/{total_chunks} chunks received")
            
            # Check if complete
            if received == total_chunks:
                print(f"All chunks received! Assembling file: {filename}")
                
                # Mark file as being assembled
                with assembling_lock:
                    assembling_files.add(filename)
                
                final_path = os.path.join(UPLOAD_FOLDER, filename)
                
                # Assemble file
                try:
                    with open(final_path, 'wb') as outfile:
                        for i in range(total_chunks):
                            chunk_path = os.path.join(TEMP_FOLDER, f"{transfer_id}_chunk_{i}")
                            
                            if not os.path.exists(chunk_path):
                                print(f"ERROR: Missing chunk {i}!")
                                raise Exception(f"Missing chunk {i}")
                            
                            with open(chunk_path, 'rb') as infile:
                                chunk_data = infile.read()
                                outfile.write(chunk_data)
                                print(f"  Wrote chunk {i}: {len(chunk_data)} bytes")
                            
                            os.remove(chunk_path)
                    
                    # Verify file size
                    final_size = os.path.getsize(final_path)
                    expected_size = sum(active_transfers[transfer_id]['chunk_sizes'].values())
                    
                    print(f"File assembled: {final_size} bytes (expected: {expected_size} bytes)")
                    
                    if final_size != expected_size:
                        print(f"WARNING: Size mismatch! Got {final_size}, expected {expected_size}")
                    
                    elapsed = time.time() - active_transfers[transfer_id]['start_time']
                    speed_mbps = (final_size / elapsed) / (1024 * 1024)
                    
                    print(f"✓✓✓ Upload complete: {filename} ({final_size / (1024*1024):.2f} MB in {elapsed:.2f}s = {speed_mbps:.2f} MB/s)")
                    
                    # Save file metadata
                    username = get_username()
                    metadata = {
                        'filename': filename,
                        'uploaded_by': username,
                        'upload_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                        'file_size': final_size,
                        'downloads': []
                    }
                    save_file_metadata(filename, metadata)
                    
                    # Mark file as ready for download
                    with assembling_lock:
                        if filename in assembling_files:
                            assembling_files.remove(filename)
                    
                    # Add system message to chat
                    add_chat_message('System', f'{username} uploaded {filename} ({final_size / (1024*1024):.2f} MB)', 'system')
                    
                    del active_transfers[transfer_id]
                    
                    return jsonify({'success': True, 'completed': True, 'speed': speed_mbps, 'size': final_size})
                
                except Exception as e:
                    print(f"ERROR assembling file: {e}")
                    
                    # Remove from assembling list
                    with assembling_lock:
                        if filename in assembling_files:
                            assembling_files.remove(filename)
                    
                    # Clean up chunks on error
                    for i in range(total_chunks):
                        chunk_path = os.path.join(TEMP_FOLDER, f"{transfer_id}_chunk_{i}")
                        if os.path.exists(chunk_path):
                            os.remove(chunk_path)
                    
                    # Remove incomplete file
                    if os.path.exists(final_path):
                        os.remove(final_path)
                    
                    if transfer_id in active_transfers:
                        del active_transfers[transfer_id]
                    
                    return jsonify({'success': False, 'error': str(e)}), 500
        
        return jsonify({'success': True, 'completed': False, 'received': received, 'total': total_chunks})
        
    except Exception as e:
        print(f"Chunk upload error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route("/delete_file/<filename>", methods=["POST"])
@login_required
def delete_file(filename):
    # Only server can delete
    if session.get('role') != 'server':
        return jsonify({"error": "Permission denied"}), 403

    filename = secure_filename(filename)
    filepath = os.path.join(UPLOAD_FOLDER, filename)
    metadata_path = get_metadata_path(filename)

    try:
        if os.path.exists(filepath):
            os.remove(filepath)

        if os.path.exists(metadata_path):
            os.remove(metadata_path)

        add_chat_message('System', f'{get_username()} deleted {filename}', 'system')

        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/admin/kick_user", methods=["POST"])
@login_required
def kick_user():
    if session.get('role') != 'server':
        return jsonify({"error": "Permission denied"}), 403

    data = request.json
    username = data.get("username")

    if not username:
        return jsonify({"error": "Invalid username"}), 400

    with kicked_lock:
        kicked_users.add(username)

    # Remove from online users immediately
    with user_lock:
        for sid in list(connected_users.keys()):
            if connected_users[sid]['username'] == username:
                del connected_users[sid]

    kicker = session.get("username", "Host")
    add_chat_message("System", f"{username} was kicked by {kicker}", "system")

    return jsonify({"success": True})

@app.route("/upload_progress")
@login_required
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
@login_required
def upload_parallel():
    # Check if username is set
    if not is_username_set():
        return redirect(url_for("files") + "?error=username_required")
    
    try:
        f = request.files["file"]
        if f.filename:
            filename = secure_filename(f.filename)
            filepath = os.path.join(UPLOAD_FOLDER, filename)
            f.save(filepath)
            print(f"File uploaded (fallback): {filename}")
            
            # Save metadata for fallback upload
            username = get_username()
            file_size = os.path.getsize(filepath)
            metadata = {
                'filename': filename,
                'uploaded_by': username,
                'upload_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'file_size': file_size,
                'downloads': []
            }
            save_file_metadata(filename, metadata)
            
            # Add system message to chat
            add_chat_message('System', f'{username} uploaded {filename}', 'system')
    except Exception as e:
        print(f"Upload error: {e}")
    
    if request.referrer and 'dashboard' in request.referrer:
        return redirect(url_for("dashboard"))
    else:
        return redirect(url_for("files"))

@app.route("/download_parallel/<filename>")
@login_required
def download_parallel(filename):
    # Check if username is set
    if not is_username_set():
        return jsonify({
            'error': 'Please set your username first before downloading files',
            'username_required': True
        }), 403
    
    filepath = os.path.join(UPLOAD_FOLDER, secure_filename(filename))
    
    # Check if file is still being assembled
    with assembling_lock:
        if filename in assembling_files:
            return jsonify({
                'error': 'File is still being assembled. Please wait...',
                'assembling': True
            }), 425  # Too Early status code
    
    if not os.path.exists(filepath):
        return "File not found", 404
    
    # Record download
    username = get_username()
    add_download_record(filename, username)
    
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

@app.route("/file_info/<filename>")
@login_required
def file_info(filename):
    """Get detailed information about a file"""
    try:
        metadata = load_file_metadata(filename)
        return jsonify(metadata)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# Chat Routes
@app.route("/chat/send", methods=["POST"])
@login_required
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
@login_required
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
@login_required
def chat_history():
    """Get all chat history"""
    with chat_lock:
        return jsonify(chat_messages)

@app.route("/chat/set_username", methods=["POST"])
@login_required
def set_username():
    """Allow users to change their display name"""
    try:
        data = request.json
        new_username = data.get('username', '').strip()
        
        if not new_username or len(new_username) > 20:
            return jsonify({'success': False, 'error': 'Invalid username'}), 400
        
        old_username = session.get('username', None)
        
        # Only update if name actually changed
        if old_username and old_username == new_username:
            return jsonify({'success': True, 'username': new_username})
        
        session['username'] = new_username
        
        # Add system message when username is changed
        if old_username:
            add_chat_message('System', f'{old_username} changed name to {new_username}', 'system')
        
        return jsonify({'success': True, 'username': new_username})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route("/chat")
@login_required
def chat_page():
    """Standalone chat page"""
    username = get_username()
    return render_template("chat_app.html", username=username)

@app.route("/file_status")
@login_required
def file_status():
    """Get status of all files (ready or assembling)"""
    with assembling_lock:
        return jsonify({
            'assembling': list(assembling_files)
        })

@app.route("/online_users")
@login_required
def online_users():
    """Get list of currently online users"""
    users = get_online_users()
    return jsonify({'users': users})

@app.route("/logout")
def logout():
    username = session.get('username', 'User')
    if 'logged_in' in session:
        add_chat_message('System', f'{username} left the chat', 'system')
    # Clear all session data
    session.clear()
    return redirect(url_for("login"))

log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

# Register shutdown handlers
def signal_handler(sig, frame):
    """Handle shutdown signals"""
    print("\n\n🛑 Server shutting down...")
    print("💾 Saving all activity to log file...")
    save_all_metadata_to_file()
    print("✓ Shutdown complete.\n")
    exit(0)

def exit_handler():
    """Handle normal exit"""
    print("\n\n🛑 Server shutting down...")
    print("💾 Saving all activity to log file...")
    save_all_metadata_to_file()
    print("✓ Shutdown complete.\n")

# Register signal handlers
signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)
atexit.register(exit_handler)

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
        print(f"   • Up to 10GB file support")
        print(f"   • 💬 Real-time chat between users")
        print(f"   • 🔐 Authentication required for all routes")
        print(f"   • 📊 Upload/Download tracking with metadata")
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
        print(f"   3. Login with admin credentials")
        print(f"")
        
        app.run(host="0.0.0.0", port=PORT, threaded=True)