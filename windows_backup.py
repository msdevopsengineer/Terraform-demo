import os
import zipfile
import shutil
import subprocess
import json
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import smtplib
import requests
from jinja2 import Environment, FileSystemLoader, select_autoescape

# ======================================================
# === Environment Variables ===
# ======================================================
sql_instance = os.getenv("sql_instance")
sql_user = os.getenv("sql_user")
sql_password = os.getenv("sql_password")
sql_databases = [db.strip() for db in os.getenv("sql_databases", "").split(",") if db.strip()]
iis_site_paths = [p.strip() for p in os.getenv("iis_site_path", "").split(",") if p.strip()]
backup_root = os.getenv("backup_dir", "C:\\backups")
os.makedirs(backup_root, exist_ok=True)

smtp_user = os.getenv("smtp_user")
smtp_password = os.getenv("smtp_password")
smtp_server = os.getenv("smtp_server")
smtp_port = int(os.getenv("smtp_port", "587"))
email_to = os.getenv("email_to")
email_from = os.getenv("email_from")

# Azure Storage vars
azure_storage_url = os.getenv("azure_storage_url")  # e.g. https://myaccount.blob.core.windows.net/backups/
azure_sas_token = os.getenv("azure_sas_token")      # e.g. ?sv=2024-11-04&sr=c&sp=racw&sig=...

# Environment label for Blob separation (e.g., 'prod', 'qa', 'dev')
server_env = os.getenv("server_env", "prod").lower().strip()

# ======================================================
# === Runtime Variables ===
# ======================================================
now = datetime.now()
timestamp = now.strftime("%d-%b-%Y_%H%M")
hostname = os.getenv("COMPUTERNAME", "WindowsServer")
log_lines = []
rc = 0

def log(msg):
    print(msg)
    log_lines.append(msg)

# ======================================================
# === SQL BACKUPS (Full - Monthly Folder) ===
# ======================================================
try:
    sql_month_folder = os.path.join(backup_root, "SQL", now.strftime("%Y-%m"))
    os.makedirs(sql_month_folder, exist_ok=True)

    for db in sql_databases:
        bak_file = os.path.join(sql_month_folder, f"{db}_{timestamp}.bak")
        log(f"[SQL] Backing up database '{db}' to {bak_file} ...")

        cmd = [
            "sqlcmd",
            "-S", sql_instance,
            "-U", sql_user,
            "-P", sql_password,
            "-Q", f"BACKUP DATABASE [{db}] TO DISK = N'{bak_file}' WITH INIT"
        ]

        result = subprocess.run(cmd, capture_output=True, text=True)

        if result.returncode == 0:
            log(f"[OK] Database '{db}' backup complete.")
        else:
            log(f"[ERROR] Failed to backup '{db}': {result.stderr.strip()}")
            rc = 1
except Exception as e:
    rc = 1
    log(f"[FATAL] SQL backup failed: {e}")

# ======================================================
# === IIS SITE BACKUPS (Incremental per Site) ===
# ======================================================
try:
    iis_root_folder = os.path.join(backup_root, "IIS")
    os.makedirs(iis_root_folder, exist_ok=True)

    for site_path in iis_site_paths:
        if not os.path.exists(site_path):
            log(f"[WARN] IIS site path not found: {site_path}")
            rc = 1
            continue

        site_name = os.path.basename(os.path.normpath(site_path))
        site_backup_dir = os.path.join(iis_root_folder, site_name)
        os.makedirs(site_backup_dir, exist_ok=True)

        manifest_path = os.path.join(site_backup_dir, f"{site_name}_manifest.json")
        incremental_zip = os.path.join(site_backup_dir, f"{site_name}_incremental_{timestamp}.zip")

        log(f"[INC] Checking IIS site '{site_name}' for changes...")

        previous_manifest = {}
        if os.path.exists(manifest_path):
            try:
                with open(manifest_path, "r") as f:
                    previous_manifest = json.load(f)
            except Exception as e:
                log(f"[WARN] Failed to load manifest for {site_name}: {e}")

        new_manifest = {}
        changed_files = []

        for root, _, files in os.walk(site_path):
            for file in files:
                file_path = os.path.join(root, file)
                rel_path = os.path.relpath(file_path, site_path)
                mtime = os.path.getmtime(file_path)
                new_manifest[rel_path] = mtime
                if rel_path not in previous_manifest or mtime > previous_manifest[rel_path]:
                    changed_files.append(file_path)

        if changed_files:
            log(f"[ZIP] {len(changed_files)} changed/new files found, creating incremental backup...")
            with zipfile.ZipFile(incremental_zip, "w", zipfile.ZIP_DEFLATED) as zipf:
                for file_path in changed_files:
                    rel_path = os.path.relpath(file_path, site_path)
                    zipf.write(file_path, rel_path)
            log(f"[OK] Incremental IIS backup created: {incremental_zip}")
        else:
            log(f"[SKIP] No file changes detected for '{site_name}'.")

        with open(manifest_path, "w") as f:
            json.dump(new_manifest, f)
            log(f"[MANIFEST] Updated manifest saved for {site_name}.")
except Exception as e:
    rc = 1
    log(f"[FATAL] IIS incremental backup failed: {e}")

# ======================================================
# === Retention Policy (SQL only - 60 days) ===
# ======================================================
try:
    retention_days = 60
    cutoff = now - timedelta(days=retention_days)
    log(f"[RETENTION] Removing SQL backups older than {retention_days} days...")

    sql_backup_root = os.path.join(backup_root, "SQL")
    for root, _, files in os.walk(sql_backup_root):
        for file in files:
            if file.lower().endswith(".bak"):
                file_path = os.path.join(root, file)
                file_time = datetime.fromtimestamp(os.path.getmtime(file_path))
                if file_time < cutoff:
                    os.remove(file_path)
                    log(f"[DELETE] Old SQL backup removed: {file_path}")
except Exception as e:
    log(f"[WARN] Retention cleanup failed: {e}")

# ======================================================
# === Upload Full Folders (IIS + SQL) to Azure Blob Storage (Env Prefix) ===
# ======================================================
def upload_file_to_blob(file_path, container_url, sas, relative_path):
    try:
        blob_url = f"{container_url.rstrip('/')}/{relative_path.replace(os.sep, '/')}{sas if sas.startswith('?') else '?' + sas}"

        headers = {
            "x-ms-blob-type": "BlockBlob",
            "x-ms-blob-cache-control": "no-cache",
            "x-ms-blob-content-type": "application/octet-stream",
            "x-ms-version": "2020-10-02",
            "x-ms-blob-content-disposition": f"attachment; filename={os.path.basename(file_path)}",
            "x-ms-overwrite": "true"
        }

        with open(file_path, "rb") as data:
            resp = requests.put(blob_url, headers=headers, data=data)

        if resp.status_code in (200, 201):
            log(f"[UPLOAD] Overwrote/Uploaded: {relative_path}")
            return 0
        else:
            log(f"[ERROR] Upload failed for {relative_path}: {resp.status_code} {resp.text}")
            return 1
    except Exception as ex:
        log(f"[ERROR] Blob upload error for {relative_path}: {ex}")
        return 1


def upload_folder_to_blob(folder_path, container_url, sas, base_folder_name):
    log(f"[AZURE] Uploading folder '{base_folder_name}' to Blob Storage (overwrite enabled)...")
    for root, _, files in os.walk(folder_path):
        for file in files:
            full_path = os.path.join(root, file)
            relative_path = os.path.join(base_folder_name, os.path.relpath(full_path, backup_root))
            rc_code = upload_file_to_blob(full_path, container_url, sas, relative_path)
            if rc_code != 0:
                global rc
                rc = 1


if azure_storage_url and azure_sas_token:
    log(f"[AZURE] Starting folder uploads for environment: {server_env.upper()} (IIS + SQL)...")

    sql_root = os.path.join(backup_root, "SQL")
    iis_root = os.path.join(backup_root, "IIS")

    if os.path.exists(sql_root):
        upload_folder_to_blob(sql_root, azure_storage_url, azure_sas_token, f"{server_env}/SQL")

    if os.path.exists(iis_root):
        upload_folder_to_blob(iis_root, azure_storage_url, azure_sas_token, f"{server_env}/IIS")

    log(f"[AZURE] All folders for environment '{server_env.upper()}' uploaded successfully.")
else:
    log("[AZURE] Azure Blob upload skipped (no credentials provided).")

# ======================================================
# === Generate and Send Email Report (Multi-Recipient Support) ===
# ======================================================
try:
    env = Environment(loader=FileSystemLoader("templates"), autoescape=select_autoescape(["html", "xml"]))
    template = env.get_template("report_email_animated.html.j2")
    html_content = template.render(
        hostname=hostname,
        environment=server_env.upper(),
        current_date=now.strftime("%d-%b-%Y"),
        current_time=now.strftime("%H:%M:%S"),
        current_year=now.strftime("%Y"),
        current_datetime=now.strftime("%Y-%m-%d %H:%M:%S"),
        script_output={"rc": rc, "stdout": "\n".join(log_lines)}
    )
except Exception as e:
    rc = 1
    html_content = f"<p><b>Backup completed, but report rendering failed:</b> {e}</p>"

try:
    recipients = [addr.strip() for addr in email_to.split(",") if addr.strip()]

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"{'✅ SUCCESS' if rc == 0 else '❌ FAILED'} - {server_env.upper()} Backup Report - {now.strftime('%d-%b-%Y %H:%M')}"
    msg["From"] = email_from
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(html_content, "html"))

    with smtplib.SMTP(smtp_server, smtp_port) as server:
        server.starttls()
        server.login(smtp_user, smtp_password)
        server.sendmail(email_from, recipients, msg.as_string())

    log(f"[OK] Email report sent successfully to: {', '.join(recipients)}")
except Exception as e:
    log(f"[ERROR] Failed to send email: {e}")
    rc = 1
