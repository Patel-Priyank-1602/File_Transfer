from flask import Flask, request, send_from_directory, render_template, redirect, url_for, session, Response, jsonify

import os
import qrcode
import io
import base64
import zipfile
import shutil
import uuid
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
app.secret_key = os.urandom(24) # Random key on every start to invalidate sessions

# Performance Configuration
CHUNK_SIZE = 2 * 1024 * 1024  # 2MB chunks for download
# No global content length limit - uploads are chunked (each chunk ~5MB)
app.config['MAX_CONTENT_LENGTH'] = None

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

# Track folder uploads in progress
folder_uploads = {}  # {folder_id: {'folder_name': str, 'files': [], 'total_size': 0, 'start_time': float}}
folder_uploads_lock = threading.Lock()

# Track folder finalization (async ZIP creation)
folder_finalize_status = {}  # {folder_id: {'status': 'processing'|'complete'|'error', ...}}
folder_finalize_lock = threading.Lock()

# Chat Configuration
chat_messages = []
chat_lock = threading.Lock()
MAX_MESSAGES = 100
connected_users = {}  # {session_id: {'username': str, 'last_seen': timestamp, 'is_server': bool}}
kicked_users = set()
kicked_lock = threading.Lock()

# Join Permission System
pending_clients = {}  # {client_id: {'name': str, 'status': 'pending'|'approved'|'rejected', 'timestamp': float, 'ip': str}}
pending_clients_lock = threading.Lock()
user_lock = threading.Lock()
USER_TIMEOUT = 30  # seconds - consider user offline after this time

# File metadata tracking
file_metadata = {}
metadata_lock = threading.Lock()

# Client History Tracking
client_history = []  # List of {name, ip, action, timestamp}
history_lock = threading.Lock()


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
            return redirect(url_for("join_page"))

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
                # Admin login always gets join approval
                session["join_approved"] = True
                
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

# ============ CLIENT JOIN PERMISSION SYSTEM ============

@app.route("/join")
def join_page():
    """Client join page - only requires entering a name"""
    # If already approved, redirect to files
    if session.get('join_approved') and session.get('logged_in'):
        return redirect(url_for('files'))
    return render_template("join.html")

@app.route("/join/request", methods=["POST"])
def join_request():
    """Client submits a join request with their name"""
    try:
        data = request.json
        client_name = data.get('name', '').strip()
        
        if not client_name:
            return jsonify({'success': False, 'error': 'Please enter your name'}), 400
        
        if len(client_name) > 20:
            return jsonify({'success': False, 'error': 'Name must be 20 characters or less'}), 400
        
        # Check if this name is kicked - MODIFIED: Allow them to request again
        # The kick is a "reset", so we don't block them here.
        # However, they are still in kicked_users until approved again.
        # with kicked_lock:
        #     if client_name in kicked_users:
        #         return jsonify({'success': False, 'error': 'You have been blocked from this session'}), 403
        
        # Generate a unique client ID
        client_id = str(uuid.uuid4())
        
        with pending_clients_lock:
            # Check if there's already a pending request with the same name
            for cid, cdata in pending_clients.items():
                if cdata['name'] == client_name and cdata['status'] == 'pending':
                    # Return existing client ID
                    return jsonify({'success': True, 'client_id': cid})
            
            pending_clients[client_id] = {
                'name': client_name,
                'status': 'pending',
                'timestamp': time.time(),
                'ip': request.remote_addr
            }
        
        print(f"🔔 Join request: {client_name} (ID: {client_id[:8]}...) from {request.remote_addr}")
        
        return jsonify({'success': True, 'client_id': client_id})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route("/join/status/<client_id>")
def join_status(client_id):
    """Client polls this to check if their join request was approved/rejected"""
    with pending_clients_lock:
        if client_id not in pending_clients:
            return jsonify({'status': 'unknown', 'error': 'Invalid request ID'}), 404
        
        client_data = pending_clients[client_id]
        status = client_data['status']
        
        if status == 'approved':
            # Set up the session for this client
            session['logged_in'] = True
            session['username'] = client_data['name']
            session['files_access_token'] = 'granted'
            session['join_approved'] = True
            session.permanent = True
            
            # Add chat message
            add_chat_message('System', f"{client_data['name']} joined the session", 'system')
            
            # Clean up
            del pending_clients[client_id]
            
            return jsonify({'status': 'approved', 'redirect': url_for('files')})
        
        elif status == 'rejected':
            # Clean up
            del pending_clients[client_id]
            return jsonify({'status': 'rejected', 'message': 'Please take permission of dashboard'})
        
        else:
            return jsonify({'status': 'pending'})

@app.route("/join/respond", methods=["POST"])
@login_required
def join_respond():
    """Dashboard approves or rejects a join request"""
    if session.get('role') != 'server':
        return jsonify({'error': 'Permission denied'}), 403
    
    try:
        data = request.json
        client_id = data.get('client_id')
        action = data.get('action')  # 'approve' or 'reject'
        
        if not client_id or action not in ('approve', 'reject'):
            return jsonify({'success': False, 'error': 'Invalid request'}), 400
        
        with pending_clients_lock:
            if client_id not in pending_clients:
                return jsonify({'success': False, 'error': 'Client request not found'}), 404
            
            if action == 'approve':
                pending_clients[client_id]['status'] = 'approved'
                client_name = pending_clients[client_id]['name']
                
                # If they were kicked, remove them from the kicked list so they can join
                with kicked_lock:
                    if client_name in kicked_users:
                        kicked_users.remove(client_name)
                
                # Add to history
                with history_lock:
                    client_history.insert(0, {
                        'name': client_name,
                        'ip': pending_clients[client_id]['ip'],
                        'action': 'Accepted',
                        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    })
                
                print(f"✅ Approved: {client_name}")
            else:
                pending_clients[client_id]['status'] = 'rejected'
                client_name = pending_clients[client_id]['name']
                
                # Add to history
                with history_lock:
                    client_history.insert(0, {
                        'name': client_name,
                        'ip': pending_clients[client_id]['ip'],
                        'action': 'Rejected',
                        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    })
                
                print(f"❌ Rejected: {client_name}")
        
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route("/join/pending")
@login_required
def join_pending():
    """Get list of pending join requests (for dashboard)"""
    if session.get('role') != 'server':
        return jsonify({'error': 'Permission denied'}), 403
    
    with pending_clients_lock:
        # Clean up old pending requests (older than 5 minutes)
        current_time = time.time()
        to_remove = [cid for cid, cdata in pending_clients.items() 
                     if current_time - cdata['timestamp'] > 300 and cdata['status'] == 'pending']
        for cid in to_remove:
            del pending_clients[cid]
        
        pending = []
        for cid, cdata in pending_clients.items():
            if cdata['status'] == 'pending':
                pending.append({
                    'client_id': cid,
                    'name': cdata['name'],
                    'ip': cdata['ip'],
                    'timestamp': datetime.fromtimestamp(cdata['timestamp']).strftime('%H:%M:%S')
                })
        
        return jsonify({'pending': pending})

@app.route("/api/history")
@login_required
def get_client_history():
    """Get history of client accept/reject actions"""
    if session.get('role') != 'server':
        return jsonify({'error': 'Permission denied'}), 403
    
    with history_lock:
        return jsonify(client_history)

# ============ END CLIENT JOIN PERMISSION SYSTEM ============

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

    url_string = f"http://{HOTSPOT_IP}:{PORT}/join"
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
        # No valid session - clear and redirect
        session.clear()
        
        # Check if request is from localhost
        is_localhost = (request.remote_addr in ['127.0.0.1', 'localhost', '::1'] or 
                        '127.0.0.1' in request.host or 
                        'localhost' in request.host)
                        
        if is_localhost:
            return redirect(url_for('login', next=request.url))
        else:
            return redirect(url_for('join_page'))
    
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
    
    # Enforce join approval for remote users
    is_localhost = (request.remote_addr in ['127.0.0.1', 'localhost', '::1'] or 
                    '127.0.0.1' in request.host or 
                    'localhost' in request.host)
    
    if not is_localhost and not session.get('join_approved'):
        return redirect(url_for('join_page'))
    
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

def get_file_icon(filename):
    """Generate SVG icon based on file type"""
    ext = filename.lower().split('.')[-1] if '.' in filename else ''
    
    # Define icon SVGs for different file types
    icons = {
        'folder': '''<svg viewBox="0 0 24 24" class="icon-folder">
            <path d="M10 4H4c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V8c0-1.1-.9-2-2-2h-8l-2-2z"/>
        </svg>''',
        'pdf': '''<svg viewBox="0 0 24 24" class="icon-pdf">
            <path d="M14,2L20,8V20A2,2 0 0,1 18,22H6A2,2 0 0,1 4,20V4A2,2 0 0,1 6,2H14M18,20V9H13V4H6V20H18M10.92,12.31C10.68,11.54 10.15,9.08 11.55,9.04C12.95,9 12.03,12.16 12.03,12.16C12.42,13.65 14.05,14.72 14.05,14.72C14.55,14.57 17.4,14.24 17,15.72C16.57,17.2 13.5,15.81 13.5,15.81C11.55,15.95 10.09,16.47 10.09,16.47C8.96,18.58 7.64,19.5 7.1,18.61C6.43,17.5 9.23,16.07 9.23,16.07C10.68,13.72 10.9,12.35 10.92,12.31Z"/>
        </svg>''',
        'video': '''<svg viewBox="0 0 24 24" class="icon-video">
            <path d="M18,4L20,8H17L15,4H13L15,8H12L10,4H8L10,8H7L5,4H4A2,2 0 0,0 2,6V18A2,2 0 0,0 4,20H20A2,2 0 0,0 22,18V4H18M4,12V10H8V12H4M4,18V14H8V18H4M14,18V14H8.5V18H14M20,18H14.5V14H20V18M20,12H10V10H20V12Z"/>
        </svg>''',
        'audio': '''<svg viewBox="0 0 24 24" class="icon-audio">
            <path d="M14,3.23V5.29C16.89,6.15 19,8.83 19,12C19,15.17 16.89,17.84 14,18.7V20.77C18,19.86 21,16.28 21,12C21,7.72 18,4.14 14,3.23M16.5,12C16.5,10.23 15.5,8.71 14,7.97V16C15.5,15.29 16.5,13.76 16.5,12M3,9V15H7L12,20V4L7,9H3Z"/>
        </svg>''',
        'image': '''<svg viewBox="0 0 24 24" class="icon-image">
            <path d="M8.5,13.5L11,16.5L14.5,12L19,18H5M21,19V5C21,3.89 20.1,3 19,3H5A2,2 0 0,0 3,5V19A2,2 0 0,0 5,21H19A2,2 0 0,0 21,19Z"/>
        </svg>''',
        'zip': '''<svg viewBox="0 0 24 24" class="icon-zip">
            <path d="M14,17H12V15H10V13H12V15H14M14,9H12V11H14V13H12V11H10V9H12V7H10V5H12V7H14M19,3A2,2 0 0,1 21,5V19A2,2 0 0,1 19,21H5A2,2 0 0,1 3,19V5A2,2 0 0,1 5,3H19M19,5H5V19H19V5Z"/>
        </svg>''',
        'document': '''<svg viewBox="0 0 24 24" class="icon-document">
            <path d="M14,2H6A2,2 0 0,0 4,4V20A2,2 0 0,0 6,22H18A2,2 0 0,0 20,20V8L14,2M18,20H6V4H13V9H18V20M9,13V11H7V13H9M9,17V15H7V17H9M15,15V17H17V15H15M15,11V13H17V11H15M11,17H13V15H11V17M11,13H13V11H11V13Z"/>
        </svg>''',
        'code': '''<svg viewBox="0 0 24 24" class="icon-code">
            <path d="M14.6,16.6L19.2,12L14.6,7.4L16,6L22,12L16,18L14.6,16.6M9.4,16.6L4.8,12L9.4,7.4L8,6L2,12L8,18L9.4,16.6Z"/>
        </svg>''',
        'default': '''<svg viewBox="0 0 24 24" class="icon-default">
            <path d="M14,2H6A2,2 0 0,0 4,4V20A2,2 0 0,0 6,22H18A2,2 0 0,0 20,20V8L14,2M18,20H6V4H13V9H18V20Z"/>
        </svg>'''
    }
    
    # Map extensions to icon types
    ext_map = {
        # PDF
        'pdf': 'pdf',
        # Video
        'mp4': 'video', 'mkv': 'video', 'avi': 'video', 'mov': 'video', 'wmv': 'video', 
        'flv': 'video', 'webm': 'video', 'm4v': 'video', '3gp': 'video',
        # Audio
        'mp3': 'audio', 'wav': 'audio', 'flac': 'audio', 'aac': 'audio', 'ogg': 'audio', 
        'wma': 'audio', 'm4a': 'audio',
        # Image
        'jpg': 'image', 'jpeg': 'image', 'png': 'image', 'gif': 'image', 'bmp': 'image', 
        'svg': 'image', 'webp': 'image', 'ico': 'image', 'tiff': 'image',
        # Archive
        'zip': 'zip', 'rar': 'zip', '7z': 'zip', 'tar': 'zip', 'gz': 'zip', 'bz2': 'zip',
        # Code
        'py': 'code', 'js': 'code', 'html': 'code', 'css': 'code', 'java': 'code', 
        'cpp': 'code', 'c': 'code', 'h': 'code', 'json': 'code', 'xml': 'code', 
        'php': 'code', 'rb': 'code', 'go': 'code', 'rs': 'code', 'ts': 'code',
        # Documents
        'doc': 'document', 'docx': 'document', 'xls': 'document', 'xlsx': 'document', 
        'ppt': 'document', 'pptx': 'document', 'txt': 'document', 'rtf': 'document', 
        'odt': 'document', 'ods': 'document', 'odp': 'document',
    }
    
    icon_type = ext_map.get(ext, 'default')
    return icons.get(icon_type, icons['default'])

@app.route("/file_arranged")
@login_required
def file_arranged():
    """Grid view file browser like Windows Explorer"""
    update_user_activity()
    
    file_list = [f for f in os.listdir(UPLOAD_FOLDER) if not f.startswith('.')]
    
    # Load metadata for all files
    files_metadata = {}
    for filename in file_list:
        files_metadata[filename] = load_file_metadata(filename)
    
    is_controller = session.get('role') == 'server'
    
    return render_template(
        "file_arranged.html", 
        files=file_list,
        files_metadata=files_metadata,
        get_file_icon=get_file_icon,
        is_controller=is_controller
    )

@app.route("/api/files")
@login_required
def api_files():
    """JSON API: return current file list with metadata and icons for live refresh"""
    file_list = [f for f in os.listdir(UPLOAD_FOLDER) if not f.startswith('.')]
    
    files_data = {}
    for filename in file_list:
        meta = load_file_metadata(filename)
        files_data[filename] = {
            'metadata': meta,
            'icon_svg': get_file_icon(filename)
        }
    
    return jsonify({'files': files_data})

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
                            
                            os.remove(chunk_path)
                    
                    # Verify file size
                    final_size = os.path.getsize(final_path)
                    expected_size = sum(active_transfers[transfer_id]['chunk_sizes'].values())
                    
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

# ============ FOLDER UPLOAD ENDPOINTS (Server-side ZIP for large folders up to 50GB) ============

@app.route("/upload_folder_start", methods=["POST"])
@login_required
def upload_folder_start():
    """Initialize a folder upload session"""
    if not is_username_set():
        return jsonify({'success': False, 'error': 'Please set your username first'}), 400
    
    try:
        data = request.json
        folder_name = secure_filename(data.get('folderName', 'folder'))
        total_files = data.get('totalFiles', 0)
        total_size = data.get('totalSize', 0)
        
        # Generate unique folder upload ID
        folder_id = str(uuid.uuid4())
        
        # Create temp directory for this folder upload
        folder_temp_path = os.path.join(TEMP_FOLDER, f"folder_{folder_id}")
        os.makedirs(folder_temp_path, exist_ok=True)
        
        with folder_uploads_lock:
            folder_uploads[folder_id] = {
                'folder_name': folder_name,
                'temp_path': folder_temp_path,
                'total_files': total_files,
                'total_size': total_size,
                'received_files': 0,
                'received_bytes': 0,
                'start_time': time.time(),
                'username': get_username()
            }
        
        print(f"Folder upload started: {folder_name} ({total_files} files, {total_size / (1024*1024):.2f} MB)")
        return jsonify({'success': True, 'folderId': folder_id})
    
    except Exception as e:
        print(f"Folder start error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route("/upload_folder_file", methods=["POST"])
@login_required
def upload_folder_file():
    """Upload a single file from a folder"""
    if not is_username_set():
        return jsonify({'success': False, 'error': 'Please set your username first'}), 400
    
    try:
        folder_id = request.form.get('folderId')
        relative_path = request.form.get('relativePath', '')
        file_chunk = request.files.get('chunk')
        chunk_index = int(request.form.get('chunkIndex', 0))
        total_chunks = int(request.form.get('totalChunks', 1))
        
        if not folder_id or folder_id not in folder_uploads:
            return jsonify({'success': False, 'error': 'Invalid folder upload session'}), 400
        
        with folder_uploads_lock:
            folder_info = folder_uploads[folder_id]
            folder_temp_path = folder_info['temp_path']
        
        # Sanitize the relative path to prevent directory traversal
        # Keep the folder structure but make safe
        path_parts = relative_path.replace('\\', '/').split('/')
        safe_parts = [secure_filename(part) for part in path_parts if part and part != '..' and part != '.']
        safe_relative_path = os.path.join(*safe_parts) if safe_parts else 'file'
        
        # Create directory structure
        file_dir = os.path.join(folder_temp_path, os.path.dirname(safe_relative_path))
        if file_dir and file_dir != folder_temp_path:
            os.makedirs(file_dir, exist_ok=True)
        
        file_path = os.path.join(folder_temp_path, safe_relative_path)
        
        # Handle chunked file upload
        if total_chunks > 1:
            # Multi-chunk file
            chunk_path = f"{file_path}.chunk_{chunk_index}"
            file_chunk.save(chunk_path)
            
            # Check if all chunks received for this file
            all_chunks_received = True
            for i in range(total_chunks):
                if not os.path.exists(f"{file_path}.chunk_{i}"):
                    all_chunks_received = False
                    break
            
            if all_chunks_received:
                # Assemble the file from chunks
                with open(file_path, 'wb') as outfile:
                    for i in range(total_chunks):
                        chunk_file = f"{file_path}.chunk_{i}"
                        with open(chunk_file, 'rb') as infile:
                            outfile.write(infile.read())
                        os.remove(chunk_file)
                
                # Update received files count
                with folder_uploads_lock:
                    folder_uploads[folder_id]['received_files'] += 1
                    folder_uploads[folder_id]['received_bytes'] += os.path.getsize(file_path)
        else:
            # Single chunk file
            file_chunk.save(file_path)
            
            with folder_uploads_lock:
                folder_uploads[folder_id]['received_files'] += 1
                folder_uploads[folder_id]['received_bytes'] += os.path.getsize(file_path)
        
        return jsonify({'success': True})
    
    except Exception as e:
        print(f"Folder file upload error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route("/upload_folder_finalize", methods=["POST"])
@login_required
def upload_folder_finalize():
    """Finalize folder upload - create ZIP on server asynchronously"""
    if not is_username_set():
        return jsonify({'success': False, 'error': 'Please set your username first'}), 400
    
    try:
        data = request.json
        folder_id = data.get('folderId')
        
        if not folder_id or folder_id not in folder_uploads:
            return jsonify({'success': False, 'error': 'Invalid folder upload session'}), 400
        
        with folder_uploads_lock:
            folder_info = folder_uploads[folder_id]
            folder_name = folder_info['folder_name']
            folder_temp_path = folder_info['temp_path']
            username = folder_info['username']
            start_time = folder_info['start_time']
        
        zip_filename = f"{folder_name}.zip"
        zip_path = os.path.join(UPLOAD_FOLDER, zip_filename)
        
        # Handle duplicate filenames
        counter = 1
        while os.path.exists(zip_path):
            zip_filename = f"{folder_name}_{counter}.zip"
            zip_path = os.path.join(UPLOAD_FOLDER, zip_filename)
            counter += 1
        
        # Mark as assembling
        with assembling_lock:
            assembling_files.add(zip_filename)
        
        # Initialize finalize status
        with folder_finalize_lock:
            folder_finalize_status[folder_id] = {
                'status': 'processing',
                'progress': 0,
                'filename': zip_filename,
                'error': None
            }
        
        print(f"Creating ZIP (async): {zip_filename}")
        
        # Run ZIP creation in background thread
        def create_zip_background():
            try:
                total_size = 0
                file_count = 0
                
                # First count total files for progress tracking
                total_files_to_zip = 0
                for root, dirs, files_in_dir in os.walk(folder_temp_path):
                    total_files_to_zip += len(files_in_dir)
                
                with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED, allowZip64=True, compresslevel=9) as zipf:
                    for root, dirs, files_in_dir in os.walk(folder_temp_path):
                        for file in files_in_dir:
                            file_path = os.path.join(root, file)
                            arcname = os.path.relpath(file_path, folder_temp_path)
                            zipf.write(file_path, arcname)
                            total_size += os.path.getsize(file_path)
                            file_count += 1
                            
                            # Update progress
                            if total_files_to_zip > 0:
                                with folder_finalize_lock:
                                    folder_finalize_status[folder_id]['progress'] = int((file_count / total_files_to_zip) * 100)
                
                # Clean up temp folder
                shutil.rmtree(folder_temp_path, ignore_errors=True)
                
                # Get final zip size
                final_size = os.path.getsize(zip_path)
                elapsed = time.time() - start_time
                speed_mbps = (total_size / elapsed) / (1024 * 1024) if elapsed > 0 else 0
                
                print(f"✓✓✓ Folder ZIP complete: {zip_filename} ({file_count} files, {final_size / (1024*1024):.2f} MB in {elapsed:.2f}s)")
                
                # Save metadata
                metadata = {
                    'filename': zip_filename,
                    'uploaded_by': username,
                    'upload_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    'file_size': final_size,
                    'downloads': [],
                    'is_folder': True,
                    'original_folder': folder_name,
                    'file_count': file_count
                }
                save_file_metadata(zip_filename, metadata)
                
                # Remove from assembling
                with assembling_lock:
                    if zip_filename in assembling_files:
                        assembling_files.remove(zip_filename)
                
                # Clean up folder upload tracking
                with folder_uploads_lock:
                    if folder_id in folder_uploads:
                        del folder_uploads[folder_id]
                
                # Add chat message
                add_chat_message('System', f'{username} uploaded folder {folder_name}/ ({file_count} files, {final_size / (1024*1024):.2f} MB)', 'system')
                
                # Update finalize status
                with folder_finalize_lock:
                    folder_finalize_status[folder_id] = {
                        'status': 'complete',
                        'progress': 100,
                        'filename': zip_filename,
                        'size': final_size,
                        'fileCount': file_count,
                        'speed': round(speed_mbps, 2),
                        'error': None
                    }
                    
            except Exception as e:
                print(f"ZIP creation error: {e}")
                import traceback
                traceback.print_exc()
                
                # Clean up on error
                with assembling_lock:
                    if zip_filename in assembling_files:
                        assembling_files.remove(zip_filename)
                
                if os.path.exists(zip_path):
                    try:
                        os.remove(zip_path)
                    except:
                        pass
                
                shutil.rmtree(folder_temp_path, ignore_errors=True)
                
                with folder_uploads_lock:
                    if folder_id in folder_uploads:
                        del folder_uploads[folder_id]
                
                with folder_finalize_lock:
                    folder_finalize_status[folder_id] = {
                        'status': 'error',
                        'progress': 0,
                        'filename': zip_filename,
                        'error': str(e)
                    }
        
        # Start background ZIP creation
        threading.Thread(target=create_zip_background, daemon=True).start()
        
        # Return immediately - client will poll for status
        return jsonify({
            'success': True,
            'async': True,
            'folderId': folder_id,
            'message': 'ZIP creation started in background'
        })
    
    except Exception as e:
        print(f"Folder finalize error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route("/upload_folder_finalize_status/<folder_id>")
@login_required
def upload_folder_finalize_status(folder_id):
    """Poll the status of async ZIP creation"""
    with folder_finalize_lock:
        if folder_id not in folder_finalize_status:
            return jsonify({'status': 'unknown', 'error': 'Invalid folder ID'}), 404
        
        status = folder_finalize_status[folder_id].copy()
        
        # Clean up completed/failed entries after returning them
        if status['status'] in ('complete', 'error'):
            # Keep for a bit so client can poll again if needed
            pass
    
    return jsonify(status)


@app.route("/folder_upload_progress/<folder_id>")
@login_required
def folder_upload_progress(folder_id):
    """Get progress of a folder upload"""
    with folder_uploads_lock:
        if folder_id not in folder_uploads:
            return jsonify({'error': 'Invalid folder ID'}), 404
        
        info = folder_uploads[folder_id]
        elapsed = time.time() - info['start_time']
        speed = (info['received_bytes'] / elapsed) / (1024 * 1024) if elapsed > 0 else 0
        
        return jsonify({
            'receivedFiles': info['received_files'],
            'totalFiles': info['total_files'],
            'receivedBytes': info['received_bytes'],
            'totalBytes': info['total_size'],
            'speed': round(speed, 2)
        })

# ============ END FOLDER UPLOAD ENDPOINTS ============

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

@app.errorhandler(413)
def request_entity_too_large(error):
    """Handle file too large error"""
    return jsonify({'success': False, 'error': 'Request too large. Please use chunked upload for large files.'}), 413


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
        print(f"   • No file size limit (chunked uploads)")
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
        print(f"   2. Open: http://{HOTSPOT_IP}:{PORT}/join")
        print(f"   3. Enter name and wait for dashboard approval")
        print(f"")
        
        app.run(host="0.0.0.0", port=PORT, threaded=True)