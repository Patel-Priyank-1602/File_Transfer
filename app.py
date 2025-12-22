import socket
from flask import Flask, request, send_from_directory, render_template_string, redirect, url_for, session, Response, jsonify
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

# ===== Credentials =====
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")

# ===== App Settings =====
app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY")

# ===== Performance Configuration =====
CHUNK_SIZE = 2 * 1024 * 1024  # 2MB chunks for parallel transfer
MAX_CONTENT_LENGTH = 2000 * 1024 * 1024  # 2GB max file size
app.config['MAX_CONTENT_LENGTH'] = MAX_CONTENT_LENGTH

# ===== Parallel Transfer Configuration =====
NUM_PARALLEL_STREAMS = 4  # Number of parallel TCP streams
STREAM_CHUNK_SIZE = 512 * 1024  # 512KB per stream chunk

# ===== Upload Folder =====
UPLOAD_FOLDER = os.getenv("UPLOAD_FOLDER", "shared_files")
TEMP_FOLDER = os.path.join(UPLOAD_FOLDER, ".temp")
if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)
if not os.path.exists(TEMP_FOLDER):
    os.makedirs(TEMP_FOLDER)

# ===== Thread pool for parallel transfers =====
executor = ThreadPoolExecutor(max_workers=NUM_PARALLEL_STREAMS * 4)

# ===== Active transfers tracking =====
active_transfers = {}
transfer_lock = threading.Lock()

# ====================================================================
# HTML TEMPLATES
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
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
        font-family: 'Poppins', Arial, sans-serif;
        display: grid;
        place-items: center;
        min-height: 100vh;
        background-color: #121212;
        color: #333;
        padding: 20px;
    }
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
    }
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
    .login-button:hover { background: #333; }
    .login-button:active { transform: scale(0.98); }
    .error-message {
        color: #d93025;
        margin-top: 20px;
        font-weight: 500;
    }
    @media (max-width: 480px) {
        .login-card { padding: 30px 20px; }
        .login-card h2 { font-size: 2rem; }
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

DASHBOARD_HTML = """
<!doctype html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>PC Dashboard - Parallel Transfer Server</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Poppins:wght@400;500;600&display=swap" rel="stylesheet">
<style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
        font-family: 'Poppins', Arial, sans-serif;
        min-height: 100vh;
        background-color: #121212;
        color: #333;
        padding: 20px;
    }
    h2, h3 { font-weight: 600; color: #000; }
    
    .dashboard-container {
        display: grid;
        grid-template-columns: 1fr 1.5fr;
        gap: 30px;
        width: 100%;
        max-width: 1400px;
        margin: 0 auto;
        background: #ffffff;
        padding: 20px 30px;
        border-radius: 12px;
        box-shadow: 0 10px 25px rgba(0, 0, 0, 0.1);
    }
    
    .qr-panel {
        padding: 10px;
        border-right: 1px solid #eee;
    }
    .qr-panel h2 {
        font-size: 2rem;
        margin-bottom: 20px;
        text-align: center;
    }
    .step-box {
        padding: 20px;
        border: 1px solid #eee;
        border-radius: 8px;
        text-align: center;
        margin-bottom: 20px;
    }
    .step-box h3 {
        font-size: 1.4rem;
        margin-bottom: 15px;
    }
    .step-box img {
        width: 100%;
        max-width: 200px;
        height: auto;
        border: 1px solid #ddd;
        border-radius: 4px;
        margin-bottom: 15px;
    }
    .step-box p {
        font-size: 1rem;
        color: #555;
    }
    .performance-info {
        background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
        color: white;
        padding: 15px;
        border-radius: 8px;
        margin-bottom: 20px;
        text-align: center;
    }
    .performance-info h4 {
        font-size: 1.1rem;
        margin-bottom: 8px;
        color: white;
    }
    .performance-info p {
        font-size: 0.9rem;
        opacity: 0.9;
    }
    .logout-section {
        text-align: center;
        margin-top: 20px;
        padding-top: 15px;
        border-top: 1px solid #eee;
    }
    .logout-link {
        color: #888;
        text-decoration: none;
        font-weight: 500;
        transition: color 0.3s ease;
        font-size: 1.1rem;
    }
    .logout-link:hover { color: #d93025; }

    .files-panel { padding: 10px; }
    
    .copy-url-section {
        margin-bottom: 20px;
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

    .files-panel h2 {
        font-size: 2rem;
        text-align: center;
        margin-bottom: 20px;
        padding-top: 15px;
        border-top: 1px solid #eee;
    }

    .upload-form {
        text-align: center;
        margin-bottom: 20px;
        padding-bottom: 20px;
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

    .progress-container {
        display: none;
        margin-top: 15px;
    }
    .progress-bar {
        width: 100%;
        height: 40px;
        background-color: #f0f0f0;
        border-radius: 8px;
        overflow: hidden;
        position: relative;
    }
    .progress-fill {
        height: 100%;
        background: linear-gradient(90deg, #667eea 0%, #764ba2 100%);
        width: 0%;
        transition: width 0.3s ease;
        display: flex;
        align-items: center;
        justify-content: center;
        color: white;
        font-weight: 600;
        font-size: 0.9rem;
    }
    .speed-display {
        margin-top: 10px;
        text-align: center;
        font-size: 0.9rem;
        color: #555;
        font-weight: 500;
    }
    .parallel-indicator {
        margin-top: 8px;
        text-align: center;
        font-size: 0.85rem;
        color: #667eea;
        font-weight: 600;
    }

    .files-header {
        font-size: 1.4rem;
        font-weight: 600;
        color: #333;
        margin-bottom: 15px;
    }
    .file-list {
        list-style: none;
        max-height: 400px;
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
    
    @media (max-width: 1024px) {
        .dashboard-container {
            grid-template-columns: 1fr;
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

        <form class="upload-form" id="uploadForm" action="/upload_parallel" method="post" enctype="multipart/form-data">
            <div class="file-input-wrapper">
                <input type="file" name="file" id="file-input" required>
                <label for="file-input" class="file-input-label">
                    <span>Click to select a file</span>
                    <span id="file-name-display"></span>
                </label>
            </div>
            <input type="submit" value="Upload File (Parallel)" class="upload-button">
            <div class="progress-container" id="progressContainer">
                <div class="progress-bar">
                    <div class="progress-fill" id="progressFill">0%</div>
                </div>
                <div class="parallel-indicator" id="parallelIndicator"></div>
                <div class="speed-display" id="speedDisplay"></div>
            </div>
        </form>

        <h3 class="files-header">Uploaded Files</h3>
        <ul class="file-list">
            {% for filename in files %}
            <li class="file-item">
                <svg class="file-icon" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor"><path d="M14,2H6A2,2 0 0,0 4,4V20A2,2 0 0,0 6,22H18A2,2 0 0,0 20,20V8L14,2M18,20H6V4H13V9H18V20Z" /></svg>
                <a href="/download_parallel/{{ filename }}">{{ filename }}</a>
            </li>
            {% else %}
            <li class="file-item" style="color: #888;">No files uploaded yet.</li>
            {% endfor %}
        </ul>
        
    </div>
</div>

<script>
function copyUrl() {
    var copyText = document.getElementById("urlToCopy");
    var copyButton = document.getElementById("copyButton");
    
    copyText.select();
    copyText.setSelectionRange(0, 99999);

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

const fileInput = document.getElementById('file-input');
const fileNameDisplay = document.getElementById('file-name-display');

fileInput.addEventListener('change', function() {
    if (this.files.length > 0) {
        fileNameDisplay.textContent = `Selected: ${this.files[0].name}`;
    } else {
        fileNameDisplay.textContent = '';
    }
});

// Parallel upload with real-time speed calculation
document.getElementById('uploadForm').addEventListener('submit', function(e) {
    e.preventDefault();
    
    const file = fileInput.files[0];
    if (!file) return;
    
    const progressContainer = document.getElementById('progressContainer');
    const progressFill = document.getElementById('progressFill');
    const speedDisplay = document.getElementById('speedDisplay');
    const parallelIndicator = document.getElementById('parallelIndicator');
    
    progressContainer.style.display = 'block';
    parallelIndicator.textContent = '🚀 Using 4 parallel streams';
    
    const chunkSize = 512 * 1024; // 512KB chunks
    const numStreams = 4;
    const totalChunks = Math.ceil(file.size / chunkSize);
    let completedChunks = 0;
    let startTime = Date.now();
    let uploadedBytes = 0;
    
    // Split file into chunks for parallel upload
    const chunksPerStream = Math.ceil(totalChunks / numStreams);
    const streams = [];
    
    for (let streamId = 0; streamId < numStreams; streamId++) {
        const startChunk = streamId * chunksPerStream;
        const endChunk = Math.min(startChunk + chunksPerStream, totalChunks);
        
        if (startChunk < totalChunks) {
            streams.push({streamId, startChunk, endChunk});
        }
    }
    
    let activeStreams = streams.length;
    
    streams.forEach(stream => {
        uploadStream(file, stream, chunkSize, function(progress, bytes) {
            completedChunks += progress;
            uploadedBytes += bytes;
            
            const percent = (completedChunks / totalChunks) * 100;
            const elapsed = (Date.now() - startTime) / 1000;
            const speed = uploadedBytes / elapsed / (1024 * 1024);
            
            progressFill.style.width = percent + '%';
            progressFill.textContent = Math.round(percent) + '%';
            speedDisplay.textContent = `Speed: ${speed.toFixed(2)} MB/s | Active Streams: ${activeStreams}`;
        }, function() {
            activeStreams--;
            if (activeStreams === 0) {
                progressFill.textContent = 'Complete!';
                setTimeout(() => window.location.reload(), 500);
            }
        });
    });
});

function uploadStream(file, stream, chunkSize, onProgress, onComplete) {
    const {streamId, startChunk, endChunk} = stream;
    let currentChunk = startChunk;
    
    function uploadNextChunk() {
        if (currentChunk >= endChunk) {
            onComplete();
            return;
        }
        
        const start = currentChunk * chunkSize;
        const end = Math.min(start + chunkSize, file.size);
        const chunk = file.slice(start, end);
        
        const formData = new FormData();
        formData.append('chunk', chunk);
        formData.append('chunkIndex', currentChunk);
        formData.append('totalChunks', Math.ceil(file.size / chunkSize));
        formData.append('filename', file.name);
        formData.append('streamId', streamId);
        
        fetch('/upload_chunk', {
            method: 'POST',
            body: formData
        })
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                onProgress(1, chunk.size);
                currentChunk++;
                uploadNextChunk();
            }
        })
        .catch(err => {
            console.error('Stream ' + streamId + ' error:', err);
            setTimeout(uploadNextChunk, 100);
        });
    }
    
    uploadNextChunk();
}
</script>
</body>
</html>
"""

FILES_HTML = """
<!doctype html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>File Server - Parallel Transfer</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Poppins:wght@400;500;600&display=swap" rel="stylesheet">
<style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
        font-family: 'Poppins', Arial, sans-serif;
        display: grid;
        place-items: center;
        min-height: 100vh;
        background-color: #121212;
        padding: 20px;
    }
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
        margin-bottom: 15px;
        color: #000;
    }
    .performance-badge {
        background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
        color: white;
        padding: 10px 20px;
        border-radius: 20px;
        text-align: center;
        margin-bottom: 25px;
        font-size: 0.9rem;
        font-weight: 600;
    }
    .upload-form {
        text-align: center;
        margin-bottom: 40px;
        padding-bottom: 30px;
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
    .progress-container {
        display: none;
        margin-top: 15px;
    }
    .progress-bar {
        width: 100%;
        height: 40px;
        background-color: #f0f0f0;
        border-radius: 8px;
        overflow: hidden;
    }
    .progress-fill {
        height: 100%;
        background: linear-gradient(90deg, #667eea 0%, #764ba2 100%);
        width: 0%;
        transition: width 0.3s ease;
        display: flex;
        align-items: center;
        justify-content: center;
        color: white;
        font-weight: 600;
        font-size: 0.9rem;
    }
    .speed-display {
        margin-top: 10px;
        text-align: center;
        font-size: 0.9rem;
        color: #555;
        font-weight: 500;
    }
    .parallel-indicator {
        margin-top: 8px;
        text-align: center;
        font-size: 0.85rem;
        color: #667eea;
        font-weight: 600;
    }
    .files-header {
        font-size: 1.4rem;
        font-weight: 600;
        color: #333;
        margin-bottom: 15px;
    }
    .file-list { list-style: none; }
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
    @media (max-width: 480px) {
        .files-container { padding: 30px 20px; }
        .files-container h2 { font-size: 1.8rem; }
    }
</style>
</head>
<body>
<div class="files-container">
    <h2>File Management</h2>
    <div class="performance-badge">🚀 Parallel Transfer Mode Active</div>

    <form class="upload-form" id="uploadForm" action="/upload_parallel" method="post" enctype="multipart/form-data">
        <div class="file-input-wrapper">
            <input type="file" name="file" id="file-input" required>
            <label for="file-input" class="file-input-label">
                <span>Click to select a file</span>
                <span id="file-name-display"></span>
            </label>
        </div>
        <input type="submit" value="Upload File" class="upload-button">
        <div class="progress-container" id="progressContainer">
            <div class="progress-bar">
                <div class="progress-fill" id="progressFill">0%</div>
            </div>
            <div class="parallel-indicator" id="parallelIndicator"></div>
            <div class="speed-display" id="speedDisplay"></div>
        </div>
    </form>

    <h3 class="files-header">Uploaded Files</h3>
    <ul class="file-list">
        {% for filename in files %}
        <li class="file-item">
            <svg class="file-icon" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor"><path d="M14,2H6A2,2 0 0,0 4,4V20A2,2 0 0,0 6,22H18A2,2 0 0,0 20,20V8L14,2M18,20H6V4H13V9H18V20Z" /></svg>
            <a href="/download_parallel/{{ filename }}">{{ filename }}</a>
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

    document.getElementById('uploadForm').addEventListener('submit', function(e) {
        e.preventDefault();
        
        const file = fileInput.files[0];
        if (!file) return;
        
        const progressContainer = document.getElementById('progressContainer');
        const progressFill = document.getElementById('progressFill');
        const speedDisplay = document.getElementById('speedDisplay');
        const parallelIndicator = document.getElementById('parallelIndicator');
        
        progressContainer.style.display = 'block';
        parallelIndicator.textContent = '🚀 Using 4 parallel streams';
        
        const chunkSize = 512 * 1024;
        const numStreams = 4;
        const totalChunks = Math.ceil(file.size / chunkSize);
        let completedChunks = 0;
        let startTime = Date.now();
        let uploadedBytes = 0;
        
        const chunksPerStream = Math.ceil(totalChunks / numStreams);
        const streams = [];
        
        for (let streamId = 0; streamId < numStreams; streamId++) {
            const startChunk = streamId * chunksPerStream;
            const endChunk = Math.min(startChunk + chunksPerStream, totalChunks);
            
            if (startChunk < totalChunks) {
                streams.push({streamId, startChunk, endChunk});
            }
        }
        
        let activeStreams = streams.length;
        
        streams.forEach(stream => {
            uploadStream(file, stream, chunkSize, function(progress, bytes) {
                completedChunks += progress;
                uploadedBytes += bytes;
                
                const percent = (completedChunks / totalChunks) * 100;
                const elapsed = (Date.now() - startTime) / 1000;
                const speed = uploadedBytes / elapsed / (1024 * 1024);
                
                progressFill.style.width = percent + '%';
                progressFill.textContent = Math.round(percent) + '%';
                speedDisplay.textContent = `Speed: ${speed.toFixed(2)} MB/s | Active: ${activeStreams} streams`;
            }, function() {
                activeStreams--;
                if (activeStreams === 0) {
                    progressFill.textContent = 'Complete!';
                    setTimeout(() => window.location.reload(), 500);
                }
            });
        });
    });

    function uploadStream(file, stream, chunkSize, onProgress, onComplete) {
        const {streamId, startChunk, endChunk} = stream;
        let currentChunk = startChunk;
        
        function uploadNextChunk() {
            if (currentChunk >= endChunk) {
                onComplete();
                return;
            }
            
            const start = currentChunk * chunkSize;
            const end = Math.min(start + chunkSize, file.size);
            const chunk = file.slice(start, end);
            
            const formData = new FormData();
            formData.append('chunk', chunk);
            formData.append('chunkIndex', currentChunk);
            formData.append('totalChunks', Math.ceil(file.size / chunkSize));
            formData.append('filename', file.name);
            formData.append('streamId', streamId);
            
            fetch('/upload_chunk', {
                method: 'POST',
                body: formData
            })
            .then(response => response.json())
            .then(data => {
                if (data.success) {
                    onProgress(1, chunk.size);
                    currentChunk++;
                    uploadNextChunk();
                }
            })
            .catch(err => {
                console.error('Stream ' + streamId + ' error:', err);
                setTimeout(uploadNextChunk, 100);
            });
        }
        
        uploadNextChunk();
    }
</script>
</body>
</html>
"""

# ===== Helper Functions =====
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

def get_file_hash(filename):
    """Generate a hash for the filename to use as transfer ID."""
    return hashlib.md5(filename.encode()).hexdigest()

# ===== Routes =====
@app.route("/", methods=["GET", "POST"])
def login():
    if "logged_in" in session:
        return redirect(url_for("dashboard"))
    error = None
    if request.method == "POST":
        if request.form["username"] == ADMIN_USERNAME and request.form["password"] == ADMIN_PASSWORD:
            session["logged_in"] = True
            return redirect(url_for("dashboard"))
        else:
            error = "Invalid username or password"
    return render_template_string(LOGIN_HTML, error=error)

@app.route("/dashboard")
def dashboard():
    if "logged_in" not in session:
        return redirect(url_for("login"))

    wifi_string = f"WIFI:T:WPA;S:{HOTSPOT_SSID};P:{HOTSPOT_PASSWORD};;"
    wifi_qr_uri = create_qr_data_uri(wifi_string)

    url_string = f"http://{HOTSPOT_IP}:{PORT}/files"
    url_qr_uri = create_qr_data_uri(url_string)
    
    file_list = os.listdir(UPLOAD_FOLDER)
    file_list = [f for f in file_list if not f.startswith('.')]

    return render_template_string(
        DASHBOARD_HTML, 
        wifi_qr=wifi_qr_uri, 
        url_qr=url_qr_uri, 
        url_string_for_copy=url_string,
        files=file_list
    )

@app.route("/files", methods=["GET"])
def files():
    file_list = os.listdir(UPLOAD_FOLDER)
    file_list = [f for f in file_list if not f.startswith('.')]
    return render_template_string(FILES_HTML, files=file_list)

@app.route("/upload_chunk", methods=["POST"])
def upload_chunk():
    """Handle individual chunk uploads from parallel streams."""
    try:
        chunk = request.files['chunk']
        chunk_index = int(request.form['chunkIndex'])
        total_chunks = int(request.form['totalChunks'])
        filename = secure_filename(request.form['filename'])
        stream_id = request.form.get('streamId', '0')
        
        transfer_id = get_file_hash(filename)
        
        # Store chunk in temporary location
        temp_chunk_path = os.path.join(TEMP_FOLDER, f"{transfer_id}_chunk_{chunk_index}")
        chunk.save(temp_chunk_path)
        
        # Track transfer progress
        with transfer_lock:
            if transfer_id not in active_transfers:
                active_transfers[transfer_id] = {
                    'filename': filename,
                    'total_chunks': total_chunks,
                    'received_chunks': set(),
                    'start_time': time.time()
                }
            
            active_transfers[transfer_id]['received_chunks'].add(chunk_index)
            received = len(active_transfers[transfer_id]['received_chunks'])
            
            # Check if all chunks received
            if received == total_chunks:
                # Assemble file from chunks
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
                
                del active_transfers[transfer_id]
                
                return jsonify({
                    'success': True,
                    'completed': True,
                    'speed': speed_mbps
                })
        
        return jsonify({
            'success': True,
            'completed': False,
            'received': received,
            'total': total_chunks
        })
        
    except Exception as e:
        print(f"Chunk upload error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route("/upload_parallel", methods=["POST"])
def upload_parallel():
    """Fallback for browsers that don't support chunked upload."""
    try:
        f = request.files["file"]
        if f.filename:
            filename = secure_filename(f.filename)
            filepath = os.path.join(UPLOAD_FOLDER, filename)
            f.save(filepath)
            print(f"File uploaded (fallback): {filename}")
    except Exception as e:
        print(f"Upload error: {e}")
    
    if request.referrer and 'dashboard' in request.referrer:
        return redirect(url_for("dashboard"))
    else:
        return redirect(url_for("files"))

@app.route("/download_parallel/<filename>")
def download_parallel(filename):
    """Serve files with support for range requests (parallel download)."""
    filepath = os.path.join(UPLOAD_FOLDER, secure_filename(filename))
    
    if not os.path.exists(filepath):
        return "File not found", 404
    
    file_size = os.path.getsize(filepath)
    
    # Check for Range header (parallel download support)
    range_header = request.headers.get('Range', None)
    
    if range_header:
        # Parse range header
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
        # Regular download
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

@app.route("/logout")
def logout():
    session.pop("logged_in", None)
    return redirect(url_for("login"))

# ===== Run Server =====
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
        print(f"╔═══════════════════════════════════════════════════════════╗")
        print(f"║   🚀 PARALLEL TRANSFER FILE SERVER - MAXIMUM SPEED 🚀    ║")
        print(f"╚═══════════════════════════════════════════════════════════╝")
        print(f"")
        print(f"⚡ Performance Features:")
        print(f"   • {NUM_PARALLEL_STREAMS} parallel TCP streams")
        print(f"   • {CHUNK_SIZE / (1024*1024):.0f}MB chunks per stream")
        print(f"   • Range request support for parallel downloads")
        print(f"   • Real-time speed monitoring")
        print(f"   • Up to 2GB file support")
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