import os
import time
import sqlite3
import threading
import subprocess
import csv
import io
import smtplib
import json
import socket
from concurrent.futures import ThreadPoolExecutor
from email.mime.text import MIMEText
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, Response

app = Flask(__name__)
ptr_executor = ThreadPoolExecutor(max_workers=5)
ptr_last_checked = {}

def resolve_ptr_and_update(device_id, ip_address):
    try:
        hostname = None
        try:
            socket.setdefaulttimeout(2.0)
            hostname, _, _ = socket.gethostbyaddr(ip_address)
        except Exception:
            pass

        # Fallback 1: nslookup (often works better for IPv6 PTR if DNS is configured)
        if not hostname:
            try:
                output = subprocess.run(['nslookup', ip_address], capture_output=True, text=True, timeout=2)
                for line in output.stdout.split('\n'):
                    if line.strip().startswith('Name:'):
                        possible_host = line.split('Name:')[1].strip()
                        if possible_host:
                            hostname = possible_host
                        break
            except Exception:
                pass

        # Fallback 2: ping -a (works well for local LLMNR/NetBIOS/mDNS names on Windows)
        if not hostname:
            try:
                cmd = ['ping', '-a', '-n', '1', '-w', '500']
                if ':' in ip_address:
                    cmd.insert(1, '-6')
                cmd.append(ip_address)
                
                output = subprocess.run(cmd, capture_output=True, text=True, timeout=2)
                for line in output.stdout.split('\n'):
                    line_lower = line.lower().strip()
                    if 'ping' in line_lower and '[' in line_lower and ']' in line_lower:
                        # Extract hostname from "Pinging hostname [IP] ..." or "Đang ping hostname [IP] ..."
                        parts = line.split('[')[0].split()
                        # Usually the hostname is the last word before the '['
                        if len(parts) >= 2:
                            possible_host = parts[-1].strip()
                            if possible_host and possible_host.lower() != ip_address.lower() and possible_host.lower() != 'ping':
                                hostname = possible_host
                        break
            except Exception:
                pass

        if hostname:
            conn = get_db_connection()
            current = conn.execute('SELECT name FROM devices WHERE id=?', (device_id,)).fetchone()
            if current and current['name'] != hostname:
                conn.execute('UPDATE devices SET name=? WHERE id=?', (hostname, device_id))
                conn.commit()
            conn.close()
    except Exception as e:
        print(f"PTR resolve error for {ip_address}: {e}")
app.secret_key = 'super_secret_key_for_flash_messages'
DB_FILE = 'monitor.db'

def get_db_connection():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    conn.execute('''
        CREATE TABLE IF NOT EXISTS devices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            ip TEXT NOT NULL UNIQUE,
            status TEXT DEFAULT 'UNKNOWN',
            last_checked TEXT,
            device_type TEXT DEFAULT 'Unknown',
            is_assigned INTEGER DEFAULT 1,
            down_since TEXT DEFAULT NULL
        )
    ''')
    # Add down_since column if missing (migration for existing DB)
    try:
        conn.execute('ALTER TABLE devices ADD COLUMN down_since TEXT DEFAULT NULL')
    except:
        pass
    # Status change log table
    conn.execute('''
        CREATE TABLE IF NOT EXISTS status_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id INTEGER,
            device_name TEXT,
            ip TEXT,
            old_status TEXT,
            new_status TEXT,
            changed_at TEXT,
            notified INTEGER DEFAULT 0,
            is_main INTEGER DEFAULT 0
        )
    ''')
    # Add is_main column if missing
    try:
        conn.execute('ALTER TABLE status_logs ADD COLUMN is_main INTEGER DEFAULT 0')
    except:
        pass
    # Notification settings table
    conn.execute('''
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    ''')
    conn.commit()
    
    # Seed default settings if not exist
    defaults = {
        'notify_enabled': '0',
        'notify_email_enabled': '0',
        'smtp_server': '',
        'smtp_port': '587',
        'smtp_user': '',
        'smtp_password': '',
        'smtp_from': '',
        'smtp_to': '',
        'notify_telegram_enabled': '0',
        'telegram_bot_token': '',
        'telegram_chat_id': '',
    }
    for key, value in defaults.items():
        conn.execute('INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)', (key, value))
    conn.commit()
    
    # Check if empty, then seed 20 dummy IPs
    count = conn.execute('SELECT COUNT(*) FROM devices').fetchone()[0]
    if count == 0:
        print("Seeding dummy IPv6 addresses...")
        dummy_ips = [
            (f'2001:db8:1::{i}', 'Unassigned', 0) for i in range(1, 21)
        ]
        conn.executemany('INSERT INTO devices (ip, device_type, is_assigned) VALUES (?, ?, ?)', dummy_ips)
        conn.commit()
        
    conn.close()

def get_setting(key, default=''):
    try:
        conn = get_db_connection()
        row = conn.execute('SELECT value FROM settings WHERE key = ?', (key,)).fetchone()
        conn.close()
        return row['value'] if row else default
    except:
        return default

def get_all_settings():
    conn = get_db_connection()
    rows = conn.execute('SELECT key, value FROM settings').fetchall()
    conn.close()
    return {row['key']: row['value'] for row in rows}

def send_notification(device_name, ip_address, old_status, new_status):
    """
    Send notification via Email and/or Telegram when a device changes status.
    """
    timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{timestamp}] ALERT: '{device_name}' ({ip_address}) {old_status} -> {new_status}")
    
    settings = get_all_settings()
    
    if settings.get('notify_enabled') != '1':
        return
    
    message = f"[IPv6 Monitor Pro] {device_name} ({ip_address}) changed from {old_status} to {new_status} at {timestamp}"
    
    # Email notification
    if settings.get('notify_email_enabled') == '1':
        try:
            smtp_server = settings.get('smtp_server', '')
            smtp_port = int(settings.get('smtp_port', '587'))
            smtp_user = settings.get('smtp_user', '')
            smtp_password = settings.get('smtp_password', '')
            smtp_from = settings.get('smtp_from', '')
            smtp_to = settings.get('smtp_to', '')
            
            if smtp_server and smtp_to:
                msg = MIMEText(message)
                msg['Subject'] = f'[ALERT] {device_name} is {new_status}'
                msg['From'] = smtp_from or smtp_user
                msg['To'] = smtp_to
                
                server = smtplib.SMTP(smtp_server, smtp_port)
                server.starttls()
                if smtp_user and smtp_password:
                    server.login(smtp_user, smtp_password)
                server.sendmail(msg['From'], smtp_to.split(','), msg.as_string())
                server.quit()
                print(f"  -> Email sent to {smtp_to}")
        except Exception as e:
            print(f"  -> Email error: {e}")
    
    # Telegram notification
    if settings.get('notify_telegram_enabled') == '1':
        try:
            import urllib.request
            bot_token = settings.get('telegram_bot_token', '')
            chat_id = settings.get('telegram_chat_id', '')
            
            if bot_token and chat_id:
                encoded_msg = urllib.parse.quote(message)
                url = f"https://api.telegram.org/bot{bot_token}/sendMessage?chat_id={chat_id}&text={encoded_msg}"
                urllib.request.urlopen(url, timeout=5)
                print(f"  -> Telegram sent to chat {chat_id}")
        except Exception as e:
            print(f"  -> Telegram error: {e}")

def log_status_change(conn, device_id, device_name, ip, old_status, new_status, notified, is_main=0):
    """Log a status change to the status_logs table."""
    current_time = time.strftime('%Y-%m-%d %H:%M:%S')
    conn.execute(
        'INSERT INTO status_logs (device_id, device_name, ip, old_status, new_status, changed_at, notified, is_main) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        (device_id, device_name, ip, old_status, new_status, current_time, notified, is_main)
    )

def ping_ip(ip_address):
    """
    Pings an IP address (IPv4 or IPv6) and returns True if UP, False if DOWN.
    Auto-detects whether the address is IPv4 or IPv6.
    """
    try:
        if ':' in ip_address:
            cmd = ['ping', '-6', '-n', '1', '-w', '1000', ip_address]
        else:
            cmd = ['ping', '-n', '1', '-w', '1000', ip_address]
        
        output = subprocess.run(
            cmd, capture_output=True, text=True, timeout=3
        )
        out_lower = output.stdout.lower()
        if output.returncode == 0:
            if "unreachable" in out_lower or "không thể truy cập" in out_lower or "could not find host" in out_lower:
                return False
            if "ms" in out_lower or "ttl=" in out_lower:
                return True
        return False
    except Exception as e:
        return False

def monitor_assigned():
    """Ping assigned (in-use) IPs every 10 seconds. Alert after 5 min continuous downtime."""
    while True:
        try:
            conn = get_db_connection()
            devices = conn.execute('SELECT id, name, ip, status, is_assigned, down_since FROM devices WHERE is_assigned = 1').fetchall()
            now_str = time.strftime('%Y-%m-%d %H:%M:%S')
            
            for device in devices:
                is_up = ping_ip(device['ip'])
                new_status = 'UP' if is_up else 'DOWN'
                old_status = device['status']
                down_since = device['down_since']
                
                if new_status == 'UP':
                    now = time.time()
                    if now - ptr_last_checked.get(device['ip'], 0) > 300:
                        ptr_last_checked[device['ip']] = now
                        ptr_executor.submit(resolve_ptr_and_update, device['id'], device['ip'])
                    # Device is online, clear down_since
                    if old_status == 'DOWN' and old_status != 'UNKNOWN':
                        # Check if it was a "Long Down" (5min+) to mark as main log
                        is_recovery_from_long_down = 0
                        last_notified_log = conn.execute(
                            'SELECT notified FROM status_logs WHERE device_id = ? ORDER BY id DESC LIMIT 1',
                            (device['id'],)
                        ).fetchone()
                        if last_notified_log and last_notified_log['notified'] == 1:
                            is_recovery_from_long_down = 1
                        
                        log_status_change(conn, device['id'], device['name'], device['ip'], 'DOWN', 'UP', 0, is_main=is_recovery_from_long_down)
                    conn.execute('UPDATE devices SET status=?, last_checked=?, down_since=NULL WHERE id=?', (new_status, now_str, device['id']))
                else:
                    # Device is DOWN
                    if old_status != 'DOWN':
                        # Just went down - record timestamp
                        conn.execute('UPDATE devices SET status=?, last_checked=?, down_since=? WHERE id=?', (new_status, now_str, now_str, device['id']))
                        # Log normal down (not main yet)
                        log_status_change(conn, device['id'], device['name'], device['ip'], old_status, 'DOWN', 0, is_main=0)
                    else:
                        # Already down - check if 5 min passed
                        conn.execute('UPDATE devices SET last_checked=? WHERE id=?', (now_str, device['id']))
                        if down_since:
                            down_time = time.mktime(time.strptime(down_since, '%Y-%m-%d %H:%M:%S'))
                            now_time = time.mktime(time.strptime(now_str, '%Y-%m-%d %H:%M:%S'))
                            elapsed = now_time - down_time
                            # At exactly 5 min mark (between 295-315s to avoid duplicate alerts)
                            if 295 <= elapsed <= 315:
                                send_notification(device['name'], device['ip'], 'UP', 'DOWN')
                                log_status_change(conn, device['id'], device['name'], device['ip'], 'UP', 'DOWN (5min)', 1, is_main=1)
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"Assigned monitor error: {e}")
        time.sleep(10)

def monitor_unassigned():
    """Ping unassigned (unused) IPs every 60 minutes."""
    while True:
        try:
            conn = get_db_connection()
            devices = conn.execute('SELECT id, name, ip, status FROM devices WHERE is_assigned = 0').fetchall()
            now_str = time.strftime('%Y-%m-%d %H:%M:%S')
            
            for device in devices:
                is_up = ping_ip(device['ip'])
                new_status = 'UP' if is_up else 'DOWN'
                if is_up:
                    now = time.time()
                    if now - ptr_last_checked.get(device['ip'], 0) > 300:
                        ptr_last_checked[device['ip']] = now
                        ptr_executor.submit(resolve_ptr_and_update, device['id'], device['ip'])
                    conn.execute('UPDATE devices SET is_assigned=1, status=?, last_checked=?, down_since=NULL WHERE id=?', (new_status, now_str, device['id']))
                    log_status_change(conn, device['id'], device['name'], device['ip'], 'NEW', 'ASSIGNED (UP)', 0, is_main=1)
                else:
                    conn.execute('UPDATE devices SET status=?, last_checked=? WHERE id=?', (new_status, now_str, device['id']))
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"Unassigned monitor error: {e}")
        time.sleep(3600)  # 60 minutes

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/scan_unassigned', methods=['POST'])
def scan_unassigned_api():
    def scan_task():
        try:
            conn = get_db_connection()
            devices = conn.execute('SELECT id, name, ip, status FROM devices WHERE is_assigned = 0').fetchall()
            now_str = time.strftime('%Y-%m-%d %H:%M:%S')
            
            for device in devices:
                is_up = ping_ip(device['ip'])
                new_status = 'UP' if is_up else 'DOWN'
                if is_up:
                    now = time.time()
                    if now - ptr_last_checked.get(device['ip'], 0) > 300:
                        ptr_last_checked[device['ip']] = now
                        ptr_executor.submit(resolve_ptr_and_update, device['id'], device['ip'])
                    conn.execute('UPDATE devices SET is_assigned=1, status=?, last_checked=?, down_since=NULL WHERE id=?', (new_status, now_str, device['id']))
                    log_status_change(conn, device['id'], device['name'], device['ip'], 'NEW', 'ASSIGNED (UP)', 0, is_main=1)
                else:
                    conn.execute('UPDATE devices SET status=?, last_checked=? WHERE id=?', (new_status, now_str, device['id']))
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"Manual unassigned scan error: {e}")

    ptr_executor.submit(scan_task)
    return jsonify({"status": "success", "message": "Quá trình quét và làm mới kho IP đã bắt đầu ở dưới nền. Vui lòng đợi trong ít phút!"})

@app.route('/api/devices')
def api_devices():
    conn = get_db_connection()
    devices = conn.execute('SELECT * FROM devices ORDER BY id DESC').fetchall()
    conn.close()
    return jsonify([dict(ix) for ix in devices])

@app.route('/api/logs')
def api_logs():
    conn = get_db_connection()
    logs = conn.execute('SELECT * FROM status_logs WHERE is_main = 1 ORDER BY id DESC LIMIT 100').fetchall()
    conn.close()
    return jsonify([dict(row) for row in logs])

@app.route('/api/logs/<int:device_id>')
def api_device_logs(device_id):
    conn = get_db_connection()
    logs = conn.execute('SELECT * FROM status_logs WHERE device_id = ? ORDER BY id DESC LIMIT 50', (device_id,)).fetchall()
    conn.close()
    return jsonify([dict(row) for row in logs])

@app.route('/api/settings')
def api_settings():
    settings = get_all_settings()
    # Mask password for security
    if settings.get('smtp_password'):
        settings['smtp_password'] = '••••••••'
    return jsonify(settings)

@app.route('/settings', methods=['POST'])
def save_settings():
    conn = get_db_connection()
    
    fields = [
        'notify_enabled', 'notify_email_enabled',
        'smtp_server', 'smtp_port', 'smtp_user', 'smtp_password',
        'smtp_from', 'smtp_to',
        'notify_telegram_enabled', 'telegram_bot_token', 'telegram_chat_id'
    ]
    
    for field in fields:
        value = request.form.get(field, '')
        # Checkbox handling
        if field in ('notify_enabled', 'notify_email_enabled', 'notify_telegram_enabled'):
            value = '1' if value == 'on' else '0'
        # Don't overwrite password if masked
        if field == 'smtp_password' and value == '••••••••':
            continue
        conn.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)', (field, value))
    
    conn.commit()
    conn.close()
    flash('Settings saved successfully!', 'success')
    return redirect(url_for('index'))

@app.route('/api/test_notification', methods=['POST'])
def test_notification():
    """Send a test notification to verify settings."""
    send_notification('Test Device', '::1', 'UP', 'DOWN')
    return jsonify({'status': 'ok', 'message': 'Test notification sent. Check your Email/Telegram.'})

@app.route('/add', methods=['POST'])
def add_device():
    name = request.form.get('name', '').strip()
    ip = request.form.get('ip', '').strip()
    # Auto-detect assignment status based on ping
    is_up = ping_ip(ip)
    is_assigned = 1 if is_up else 0
    status = 'UP' if is_up else 'DOWN'
    last_checked = time.strftime('%Y-%m-%d %H:%M:%S')

    if not ip:
        flash('IP is required!', 'error')
        return redirect(url_for('index'))

    conn = get_db_connection()
    try:
        cursor = conn.execute(
            'INSERT INTO devices (name, ip, is_assigned, status, last_checked) VALUES (?, ?, ?, ?, ?)', 
            (name, ip, is_assigned, status, last_checked)
        )
        device_id = cursor.lastrowid
        conn.commit()
        
        # Log "Assigned" event if UP when added
        if is_assigned:
            log_status_change(conn, device_id, name, ip, 'NEW', 'ASSIGNED (UP)', 0, is_main=1)
            flash(f'Device {ip} is UP and added to "IPs In Use" list.', 'success')
        else:
            flash(f'Device {ip} is DOWN and added to "Unused IPs" list.', 'info')
        conn.commit()
        if is_up:
            ptr_executor.submit(resolve_ptr_and_update, device_id, ip)
    except sqlite3.IntegrityError:
        flash('IP address already exists!', 'error')
    finally:
        conn.close()

    return redirect(url_for('index'))

@app.route('/import', methods=['POST'])
def import_devices():
    bulk_text = request.form.get('bulk_text', '').strip()
    file = request.files.get('csv_file')
    
    ips_to_add = []
    
    # Handle CSV File
    if file and file.filename != '':
        stream = io.StringIO(file.stream.read().decode("UTF8"), newline=None)
        csv_input = csv.reader(stream)
        for row in csv_input:
            if not row: continue
            ip = row[0].strip()
            name = row[1].strip() if len(row) > 1 else ''
            if ip:
                ips_to_add.append((name, ip))
                
    # Handle Text Area
    if bulk_text:
        lines = bulk_text.split('\n')
        for line in lines:
            parts = [p.strip() for p in line.split(',')]
            if parts and parts[0]:
                ip = parts[0]
                name = parts[1] if len(parts) > 1 else ''
                ips_to_add.append((name, ip))
    
    added_count = 0
    if ips_to_add:
        conn = get_db_connection()
        now_str = time.strftime('%Y-%m-%d %H:%M:%S')
        for name, ip in ips_to_add:
            try:
                # Auto-ping for import too
                is_up = ping_ip(ip)
                is_ass = 1 if is_up else 0
                status = 'UP' if is_up else 'DOWN'
                
                cursor = conn.execute(
                    'INSERT INTO devices (name, ip, is_assigned, status, last_checked) VALUES (?, ?, ?, ?, ?)', 
                    (name, ip, is_ass, status, now_str)
                )
                device_id = cursor.lastrowid
                if is_ass:
                    log_status_change(conn, device_id, name, ip, 'NEW', 'ASSIGNED (UP)', 0, is_main=1)
                if is_up:
                    ptr_executor.submit(resolve_ptr_and_update, device_id, ip)
                added_count += 1
            except sqlite3.IntegrityError:
                pass
        conn.commit()
        conn.close()
        
    flash(f'Successfully imported {added_count} devices.', 'success')
    return redirect(url_for('index'))

@app.route('/export')
def export_devices():
    conn = get_db_connection()
    devices = conn.execute('SELECT ip, name, is_assigned, status, last_checked FROM devices').fetchall()
    conn.close()
    
    si = io.StringIO()
    cw = csv.writer(si)
    cw.writerow(['IP', 'Name', 'Is Assigned (1/0)', 'Status', 'Last Checked'])
    for d in devices:
        cw.writerow([d['ip'], d['name'], d['is_assigned'], d['status'], d['last_checked']])
        
    return Response(
        si.getvalue(),
        mimetype="text/csv",
        headers={"Content-disposition": "attachment; filename=ipv6_export.csv"}
    )

@app.route('/delete/<int:id>', methods=['POST'])
def delete_device(id):
    conn = get_db_connection()
    conn.execute('DELETE FROM devices WHERE id = ?', (id,))
    conn.commit()
    conn.close()
    flash('Device removed.', 'success')
    return redirect(url_for('index'))

@app.route('/edit/<int:id>', methods=['POST'])
def edit_device(id):
    name = request.form.get('name', '').strip()
    ip = request.form.get('ip', '').strip()

    if not ip:
        flash('IP is required!', 'error')
        return redirect(url_for('index'))

    conn = get_db_connection()
    try:
        conn.execute(
            'UPDATE devices SET name = ?, ip = ? WHERE id = ?',
            (name, ip, id)
        )
        conn.commit()
        flash('Device updated successfully.', 'success')
    except sqlite3.IntegrityError:
        flash('IP address already exists!', 'error')
    finally:
        conn.close()
    return redirect(url_for('index'))

if __name__ == '__main__':
    init_db()
    # Start separate monitoring threads
    threading.Thread(target=monitor_assigned, daemon=True).start()
    threading.Thread(target=monitor_unassigned, daemon=True).start()
    app.run(debug=True, use_reloader=False, host='0.0.0.0', port=5000)
