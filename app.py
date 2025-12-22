import socket
from flask import Flask, request, send_from_directory, render_template_string, redirect, url_for, session
import os
import qrcode
import io
import base64
from dotenv import load_dotenv
from waitress import serve

# ====================================================================
# LOAD ENVIRONMENT VARIABLES
# ====================================================================
load_dotenv()

# ====================================================================
# CONFIGURATION LOADED FROM .env
# ====================================================================
HOTSPOT_SSID = os.getenv("HOTSPOT_SSID")
HOTSPOT_PASSWORD = os.getenv("HOTSPOT_PASSWORD")
HOTSPOT_IP = os.getenv("HOTSPOT_IP")
PORT = int(os.getenv("PORT", 8000))

# ===== Credentials (CHANGED: Using ADMIN_USERNAME and ADMIN_PASSWORD) =====
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")

# ===== App Settings =====
app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 1024 * 1024 * 1024  # 1GB
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0
app.secret_key = os.getenv("SECRET_KEY")

# ===== Upload Folder =====
UPLOAD_FOLDER = os.getenv("UPLOAD_FOLDER", "shared_files")
if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

# ====================================================================
# (Your HTML Templates remain exactly the same)
# ====================================================================

LOGIN_HTML = """
<!doctype html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Login - Secure Access</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Poppins:wght@400;600&display=swap" rel="stylesheet">
<style>
    /* --- Reset & Base Styles --- */
    * {
        box-sizing: border-box;
        margin: 0;
        padding: 0;
    }
    body {
        font-family: 'Poppins', Arial, sans-serif;
        display: grid;
        place-items: center;
        min-height: 100vh;
        background-color: #121212; /* Dark background */
        color: #333;
        padding: 20px; /* Ensures card doesn't touch screen edges on mobile */
    }
    /* --- Login Card --- */
    .login-card {
        width: 100%;
        max-width: 400px;
        background: #ffffff;
        padding: 40px 30px;
        border-radius: 12px;
        box-shadow: 0 10px 25px rgba(0, 0, 0, 0.1);
        text-align: center;
    }
    .login-card h2 {
        font-size: 2.5rem;
        font-weight: 600;
        margin-bottom: 25px;
        color: #000;
        transition: font-size 0.3s ease;
    }
    /* --- Form Styling --- */
    .form-group {
        position: relative;
        margin-bottom: 20px;
        text-align: left;
    }
    .form-group label {
        display: block;
        margin-bottom: 8px;
        font-weight: 500;
        color: #555;
    }
    .form-input {
        width: 100%;
        padding: 12px 15px;
        border: 1px solid #ccc;
        border-radius: 8px;
        font-size: 1rem;
        font-family: 'Poppins', sans-serif;
        transition: border-color 0.3s ease, box-shadow 0.3s ease;
    }
    .form-input:focus {
        outline: none;
        border-color: #000;
        box-shadow: 0 0 0 3px rgba(0, 0, 0, 0.1);
    }
    .login-button {
        width: 100%;
        padding: 12px 20px;
        border: none;
        border-radius: 8px;
        background: #000;
        color: white;
        font-size: 1.1rem;
        font-weight: 600;
        cursor: pointer;
        transition: background-color 0.3s ease, transform 0.2s ease;
        margin-top: 10px;
    }
    .login-button:hover {
        background: #333;
    }
    .login-button:active {
        transform: scale(0.98);
    }
    /* --- Error Message --- */
    .error-message {
        color: #d93025;
        margin-top: 20px;
        font-weight: 500;
    }
    /* --- Mobile Responsive Adjustments --- */
    @media (max-width: 480px) {
        .login-card {
            padding: 30px 20px; /* Reduce padding on smaller screens */
        }
        .login-card h2 {
            font-size: 2rem; /* Reduce heading size for mobile */
        }
    }
</style>
</head>
<body>
<div class="login-card">
    <h2>Welcome Back</h2>
    <form method="POST">
        <div class="form-group">
            <label for="username">Username</label>
            <input type="text" id="username" name="username" class="form-input" placeholder="Enter your username" required>
        </div>
        <div class="form-group">
            <label for="password">Password</label>
            <input type="password" id="password" name="password" class="form-input" placeholder="Enter your password" required>
        </div>
        <input type="submit" value="Login" class="login-button">
    </form>
    
    {% if error %}
    <p class="error-message">{{ error }}</p>
    {% endif %}
</div>
</body>
</html>
"""

# ====================================================================
# DASHBOARD HTML (with reduced height)
# ====================================================================
DASHBOARD_HTML = """
<!doctype html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>PC Dashboard - File Server</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Poppins:wght@400;500;600&display=swap" rel="stylesheet">
<style>
    /* --- Reset & Base Styles --- */
    * {
        box-sizing: border-box;
        margin: 0;
        padding: 0;
    }
    body {
        font-family: 'Poppins', Arial, sans-serif;
        min-height: 100vh;
        background-color: #121212; /* Dark background */
        color: #333;
        padding: 20px;
    }
    h2, h3 {
        font-weight: 600;
        color: #000;
    }
    
    /* --- Layout --- */
    .dashboard-container {
        display: grid;
        grid-template-columns: 1fr 1.5fr; /* Original Width */
        gap: 30px;
        width: 100%;
        max-width: 1400px; /* Original Width */
        margin: 0 auto;
        background: #ffffff;
        padding: 20px 30px; /* MODIFIED: Reduced top/bottom padding */
        border-radius: 12px;
        box-shadow: 0 10px 25px rgba(0, 0, 0, 0.1);
    }
    
    /* --- Left Panel: QR Codes --- */
    .qr-panel {
        padding: 10px; /* MODIFIED: Reduced padding */
        border-right: 1px solid #eee;
    }
    .qr-panel h2 {
        font-size: 2rem;
        margin-bottom: 20px; /* MODIFIED: Reduced margin */
        text-align: center;
    }
    .step-box {
        padding: 20px;
        border: 1px solid #eee;
        border-radius: 8px;
        text-align: center;
        margin-bottom: 20px; /* MODIFIED: Reduced margin */
    }
    .step-box h3 {
        font-size: 1.4rem;
        margin-bottom: 15px;
    }
    .step-box img {
        width: 100%;
        max-width: 200px; /* MODIFIED: Was 250px, made QR code smaller */
        height: auto;
        border: 1px solid #ddd;
        border-radius: 4px;
        margin-bottom: 15px;
    }
    .step-box p {
        font-size: 1rem;
        color: #555;
    }
    .logout-section {
        text-align: center;
        margin-top: 20px; /* MODIFIED: Reduced margin */
        padding-top: 15px; /* MODIFIED: Reduced padding */
        border-top: 1px solid #eee;
    }
    .logout-link {
        color: #888;
        text-decoration: none;
        font-weight: 500;
        transition: color 0.3s ease;
        font-size: 1.1rem;
    }
    .logout-link:hover {
        color: #d93025; /* Red for emphasis on hover */
    }

    /* --- Right Panel: Files --- */
    .files-panel {
        padding: 10px; /* MODIFIED: Reduced padding */
    }
    
    /* Copy URL Section (from old QR_HTML) */
    .copy-url-section {
        margin-bottom: 20px; /* MODIFIED: Reduced margin */
        text-align: left;
    }
    .copy-url-section p {
        font-size: 1rem;
        font-weight: 500;
        color: #555;
        margin-bottom: 10px;
    }
    .copy-input-wrapper {
        display: flex;
        width: 100%;
    }
    .copy-input-wrapper input[type="text"] {
        flex-grow: 1;
        padding: 10px 15px;
        font-family: 'Poppins', monospace;
        font-size: 1rem;
        border: 1px solid #ccc;
        border-radius: 8px 0 0 8px;
        background-color: #f7f7f7;
        color: #333;
        border-right: none;
        outline: none;
    }
    .copy-input-wrapper button {
        padding: 10px 20px;
        border: 1px solid #000;
        border-radius: 0 8px 8px 0;
        background: #000;
        color: white;
        font-family: 'Poppins', sans-serif;
        font-size: 1rem;
        font-weight: 600;
        cursor: pointer;
        transition: all 0.3s ease;
    }
    .copy-input-wrapper button:hover { background: #333; }
    .copy-input-wrapper button:active { transform: scale(0.98); }

    /* File Server Section (from FILES_HTML) */
    .files-panel h2 {
        font-size: 2rem;
        text-align: center;
        margin-bottom: 20px; /* MODIFIED: Reduced margin */
        padding-top: 15px; /* MODIFIED: Reduced padding */
        border-top: 1px solid #eee;
    }

    /* Upload Form */
    .upload-form {
        text-align: center;
        margin-bottom: 20px; /* MODIFIED: Reduced margin */
        padding-bottom: 20px; /* MODIFIED: Reduced padding */
        border-bottom: 1px solid #eee;
    }
    .file-input-wrapper {
        position: relative;
        margin-bottom: 20px;
    }
    .file-input-wrapper input[type="file"] {
        position: absolute;
        width: 0.1px;
        height: 0.1px;
        opacity: 0;
        overflow: hidden;
        z-index: -1;
    }
    .file-input-label {
        display: flex;
        flex-direction: column;
        justify-content: center;
        align-items: center;
        height: 120px;
        padding: 20px;
        background-color: #f7f7f7;
        border: 2px dashed #ccc;
        border-radius: 8px;
        cursor: pointer;
        transition: background-color 0.3s ease, border-color 0.3s ease;
        font-weight: 500;
        color: #555;
    }
    .file-input-label:hover {
        background-color: #f0f0f0;
        border-color: #999;
    }
    .file-input-label span { font-size: 1rem; }
    #file-name-display {
        font-size: 0.9rem;
        margin-top: 8px;
        color: #007BFF;
        font-weight: 600;
    }
    .upload-button {
        width: 100%;
        max-width: 250px;
        padding: 12px 20px;
        border: none;
        border-radius: 8px;
        background: #000;
        color: white;
        font-size: 1.1rem;
        font-weight: 600;
        cursor: pointer;
        transition: background-color 0.3s ease, transform 0.2s ease;
    }
    .upload-button:hover { background: #333; }
    .upload-button:active { transform: scale(0.98); }

    /* File List */
    .files-header {
        font-size: 1.4rem;
        font-weight: 600;
        color: #333;
        margin-bottom: 15px;
    }
    .file-list {
        list-style: none;
        max-height: 400px; /* Add scroll for many files */
        overflow-y: auto;
    }
    .file-item {
        display: flex;
        align-items: center;
        padding: 15px;
        border-radius: 8px;
        transition: background-color 0.3s ease;
    }
    .file-item:not(:last-child) {
        border-bottom: 1px solid #f0f0f0;
    }
    .file-item:hover {
        background-color: #f9f9f9;
    }
    .file-icon {
        width: 24px;
        height: 24px;
        margin-right: 15px;
        flex-shrink: 0;
    }
    .file-item a {
        text-decoration: none;
        color: #007BFF;
        font-weight: 500;
        word-break: break-all;
    }
    .file-item a:hover {
        text-decoration: underline;
    }
    
    /* --- Responsive --- */
    @media (max-width: 1024px) {
        .dashboard-container {
            grid-template-columns: 1fr; /* Stack columns on smaller screens */
        }
        .qr-panel {
            border-right: none;
            border-bottom: 1px solid #eee;
            padding-bottom: 30px;
        }
    }
    @media (max-width: 480px) {
        body { padding: 10px; }
        .dashboard-container { padding: 20px 15px; }
        .qr-panel h2, .files-panel h2 { font-size: 1.8rem; }
    }
</style>
</head>
<body>

<div class="dashboard-container">
    
    <div class="qr-panel">
        <h2>Connect Device</h2>
        <div class="step-box">
            <h3>Step 1: Connect to Hotspot</h3>
            <img src="{{ wifi_qr }}" alt="Wi-Fi QR Code">
            <p>Scan this with your phone's camera to join the Wi-Fi hotspot.</p>
        </div>
        
        <div class="step-box">
            <h3>Step 2: Open File Server</h3>
            <img src="{{ url_qr }}" alt="URL QR Code">
            <p>After connecting, scan this to open the file page in your browser.</p>
        </div>
        
        <div class="logout-section">
            <a href="/logout" class="logout-link">Logout from this PC</a>
        </div>
    </div>
    
    <div class="files-panel">
        
        <div class="copy-url-section">
            <p>Or, share this URL with connected devices:</p>
            <div class="copy-input-wrapper">
                <input type="text" value="{{ url_string_for_copy }}" id="urlToCopy" readonly>
                <button onclick="copyUrl()" id="copyButton">Copy</button>
            </div>
        </div>
        
        <h2>File Management</h2>

        <form class="upload-form" action="/upload" method="post" enctype="multipart/form-data">
            <div class="file-input-wrapper">
                <input type="file" name="file" id="file-input" required>
                <label for="file-input" class="file-input-label">
                    <span>Click to select a file</span>
                    <span id="file-name-display"></span>
                </label>
            </div>
            <input type="submit" value="Upload File" class="upload-button">
        </form>

        <h3 class="files-header">Uploaded Files</h3>
        <ul class="file-list">
            {% for filename in files %}
            <li class="file-item">
                <svg class="file-icon" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor"><path d="M14,2H6A2,2 0 0,0 4,4V20A2,2 0 0,0 6,22H18A2,2 0 0,0 20,20V8L14,2M18,20H6V4H13V9H18V20Z" /></svg>
                <a href="/files/{{ filename }}">{{ filename }}</a>
            </li>
            {% else %}
            <li class="file-item" style="color: #888;">No files uploaded yet.</li>
            {% endfor %}
        </ul>
        
    </div>
</div>

<script>
// --- Copy URL Function ---
function copyUrl() {
    var copyText = document.getElementById("urlToCopy");
    var copyButton = document.getElementById("copyButton");
    
    copyText.select();
    copyText.setSelectionRange(0, 99999); // For mobile devices

    try {
        var successful = document.execCommand('copy');
        
        if (successful) {
            copyButton.textContent = "Copied!";
        } else {
            copyButton.textContent = "Failed";
        }
    } catch (err) {
        console.error('Could not copy text: ', err);
        copyButton.textContent = "Error";
    }
    
    setTimeout(function() {
        copyButton.textContent = "Copy";
    }, 2000);
}

// --- File Input Display Function ---
const fileInput = document.getElementById('file-input');
const fileNameDisplay = document.getElementById('file-name-display');

fileInput.addEventListener('change', function() {
    if (this.files.length > 0) {
        fileNameDisplay.textContent = `Selected: ${this.files[0].name}`;
    } else {
        fileNameDisplay.textContent = '';
    }
});
</script>
</body>
</html>
"""

# This is the simple HTML for the mobile clients
FILES_HTML = """
<!doctype html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>File Server - Dashboard</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Poppins:wght@400;500;600&display=swap" rel="stylesheet">
<style>
    /* --- Reset & Base Styles --- */
    * {
        box-sizing: border-box;
        margin: 0;
        padding: 0;
    }
    body {
        font-family: 'Poppins', Arial, sans-serif;
        display: grid;
        place-items: center;
        min-height: 100vh;
        background-color: #121212; /* Dark background */
        padding: 20px;
    }
    /* --- Main Card Container --- */
    .files-container {
        width: 100%;
        max-width: 700px;
        background: #ffffff;
        padding: 40px 30px;
        border-radius: 12px;
        box-shadow: 0 10px 25px rgba(0, 0, 0, 0.1);
    }
    .files-container h2 {
        text-align: center;
        font-size: 2.2rem;
        font-weight: 600;
        margin-bottom: 30px;
        color: #000;
    }
    /* --- Upload Form --- */
    .upload-form {
        text-align: center;
        margin-bottom: 40px;
        padding-bottom: 30px;
        border-bottom: 1px solid #eee;
    }
    /* --- Custom File Input --- */
    .file-input-wrapper {
        position: relative;
        margin-bottom: 20px;
    }
    .file-input-wrapper input[type="file"] {
        position: absolute;
        width: 0.1px;
        height: 0.1px;
        opacity: 0;
        overflow: hidden;
        z-index: -1;
    }
    .file-input-label {
        display: flex;
        flex-direction: column;
        justify-content: center;
        align-items: center;
        height: 120px;
        padding: 20px;
        background-color: #f7f7f7;
        border: 2px dashed #ccc;
        border-radius: 8px;
        cursor: pointer;
        transition: background-color 0.3s ease, border-color 0.3s ease;
        font-weight: 500;
        color: #555;
    }
    .file-input-label:hover {
        background-color: #f0f0f0;
        border-color: #999;
    }
    .file-input-label span {
        font-size: 1rem;
    }
    #file-name-display {
        font-size: 0.9rem;
        margin-top: 8px;
        color: #007BFF;
        font-weight: 600;
    }
    /* --- Buttons --- */
    .upload-button {
        width: 100%;
        max-width: 250px;
        padding: 12px 20px;
        border: none;
        border-radius: 8px;
        background: #000;
        color: white;
        font-size: 1.1rem;
        font-weight: 600;
        cursor: pointer;
        transition: background-color 0.3s ease, transform 0.2s ease;
    }
    .upload-button:hover { background: #333; }
    .upload-button:active { transform: scale(0.98); }
    /* --- File List --- */
    .files-header {
        font-size: 1.4rem;
        font-weight: 600;
        color: #333;
        margin-bottom: 15px;
    }
    .file-list {
        list-style: none;
    }
    .file-item {
        display: flex;
        align-items: center;
        padding: 15px;
        border-radius: 8px;
        transition: background-color 0.3s ease;
    }
    .file-item:not(:last-child) {
        border-bottom: 1px solid #f0f0f0;
    }
    .file-item:hover {
        background-color: #f9f9f9;
    }
    .file-icon {
        width: 24px;
        height: 24px;
        margin-right: 15px;
        flex-shrink: 0;
    }
    .file-item a {
        text-decoration: none;
        color: #007BFF;
        font-weight: 500;
        word-break: break-all; /* Prevents long filenames from overflowing */
    }
    .file-item a:hover {
        text-decoration: underline;
    }
    /* --- Logout Section --- */
    .logout-section {
        text-align: center;
        margin-top: 30px;
        padding-top: 20px;
        border-top: 1px solid #eee;
    }
    .logout-link {
        color: #888;
        text-decoration: none;
        font-weight: 500;
        transition: color 0.3s ease;
    }
    .logout-link:hover {
        color: #d93025; /* Red for emphasis on hover */
    }
    /* --- Mobile Responsive Adjustments --- */
    @media (max-width: 480px) {
        .files-container {
            padding: 30px 20px;
        }
        .files-container h2 {
            font-size: 1.8rem;
        }
    }
</style>
</head>
<body>
<div class="files-container">
    <h2>File Management</h2>

    <form class="upload-form" action="/upload" method="post" enctype="multipart/form-data">
        <div class="file-input-wrapper">
            <input type="file" name="file" id="file-input" required>
            <label for="file-input" class="file-input-label">
                <span>Click to select a file</span>
                <span id="file-name-display"></span>
            </label>
        </div>
        <input type="submit" value="Upload File" class="upload-button">
    </form>

    <h3 class="files-header">Uploaded Files</h3>
    <ul class="file-list">
        {% for filename in files %}
        <li class="file-item">
            <svg class="file-icon" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor"><path d="M14,2H6A2,2 0 0,0 4,4V20A2,2 0 0,0 6,22H18A2,2 0 0,0 20,20V8L14,2M18,20H6V4H13V9H18V20Z" /></svg>
            <a href="/files/{{ filename }}">{{ filename }}</a>
        </li>
        {% else %}
        <li class="file-item" style="color: #888;">No files uploaded yet.</li>
        {% endfor %}
    </ul>
</div>
<script>
    const fileInput = document.getElementById('file-input');
    const fileNameDisplay = document.getElementById('file-name-display');
    
    fileInput.addEventListener('change', function() {
        if (this.files.length > 0) {
            fileNameDisplay.textContent = `Selected: ${this.files[0].name}`;
        } else {
            fileNameDisplay.textContent = '';
        }
    });
</script>
</body>
</html>
"""

# ===== Helper to generate QR code as data URI =====
def create_qr_data_uri(data):
    """Generates a QR code and returns it as a base64 data URI."""
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=4,
    )
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    byte_data = buf.getvalue()
    
    base64_data = base64.b64encode(byte_data).decode('utf-8')
    return f"data:image/png;base64,{base64_data}"

# ===== Routes =====
@app.route("/", methods=["GET", "POST"])
def login():
    if "logged_in" in session:
        return redirect(url_for("dashboard"))
    error = None
    if request.method == "POST":
        
        # --- DEBUGGING ---
        print(f"--- DEBUGGING LOGIN ---")
        print(f"Username from .env: '{ADMIN_USERNAME}' | Password from .env: '{ADMIN_PASSWORD}'")
        print(f"Username from form: '{request.form['username']}' | Password from form: '{request.form['password']}'")
        # ----------------------------------------

        if request.form["username"] == ADMIN_USERNAME and request.form["password"] == ADMIN_PASSWORD:
            session["logged_in"] = True
            return redirect(url_for("dashboard"))
        else:
            error = "Invalid username or password"
    return render_template_string(LOGIN_HTML, error=error)

@app.route("/dashboard")
def dashboard():
    # This page is protected. Only the PC user who logs in sees it.
    if "logged_in" not in session:
        return redirect(url_for("login"))

    # 1. Generate Wi-Fi QR Code String
    wifi_string = f"WIFI:T:WPA;S:{HOTSPOT_SSID};P:{HOTSPOT_PASSWORD};;"
    wifi_qr_uri = create_qr_data_uri(wifi_string)

    # 2. Generate URL QR Code String (links to /files)
    url_string = f"http://{HOTSPOT_IP}:{PORT}/files"
    url_qr_uri = create_qr_data_uri(url_string)
    
    # 3. Get file list (for the dashboard's file management)
    file_list = os.listdir(UPLOAD_FOLDER)

    # Pass all data to the new dashboard template
    return render_template_string(
        DASHBOARD_HTML, 
        wifi_qr=wifi_qr_uri, 
        url_qr=url_qr_uri, 
        url_string_for_copy=url_string,
        files=file_list
    )

@app.route("/files", methods=["GET"])
def files():
    # *** LOGIN CHECK REMOVED ***
    # This is the simple page for mobile clients.
    file_list = os.listdir(UPLOAD_FOLDER)
    return render_template_string(FILES_HTML, files=file_list)

@app.route("/upload", methods=["POST"])
def upload():
    file = request.files["file"]
    save_path = os.path.join(UPLOAD_FOLDER, file.filename)

    with open(save_path, "wb") as f:
        while True:
            chunk = file.stream.read(8192 * 8)  # 64KB chunks
            if not chunk:
                break
            f.write(chunk)

    if request.referrer and 'dashboard' in request.referrer:
        return redirect(url_for("dashboard"))
    return redirect(url_for("files"))

@app.route("/files/<filename>")
def serve_file(filename):
    # *** LOGIN CHECK REMOVED ***
    # Anyone can download a file.
    return send_from_directory(
    UPLOAD_FOLDER,
    filename,
    as_attachment=True,
    conditional=True
)

@app.route("/logout")
def logout():
    # This is for the PC user.
    session.pop("logged_in", None)
    return redirect(url_for("login"))

# ===== Run Server =====
if __name__ == "__main__":
    # Check if default values are still being used
    if HOTSPOT_SSID == "Priyank_" or HOTSPOT_PASSWORD == "12345678":
        print("="*50)
        print("!!! WARNING: Please edit your .env file !!!")
        print("You are using the default HOTSPOT_SSID or HOTSPOT_PASSWORD.")
        print("="*50)
    
    # Check if any variables are missing
    if not all([HOTSPOT_SSID, HOTSPOT_PASSWORD, HOTSPOT_IP, PORT, ADMIN_USERNAME, ADMIN_PASSWORD, app.secret_key]):
        print("="*50)
        print("!!! ERROR: Missing configuration !!!")
        print("One or more variables are not set in your .env file.")
        print("Please check your .env file and ensure all variables are set.")
        print("="*50)
    else:
        print(f"Server starting...")
        print(f"1. Make sure your PC's hotspot is ON.")
        print(f"   (SSID: {HOTSPOT_SSID})")
        print(f"2. Access the server from THIS PC at:")
        print(f"   http://127.0.0.1:{PORT}")
        print(f"After logging in, you will get the PC Dashboard.")
        
        serve(app, host="0.0.0.0", port=PORT, threads=8)