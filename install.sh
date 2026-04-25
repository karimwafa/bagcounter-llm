#!/bin/bash
# =============================================================================
#  BCLLM — Bag Counter LLM  |  Auto Installer (Self-Contained)
#  Dijalankan sebagai root di server Ubuntu/Debian baru
#  Usage: sudo bash install.sh
# =============================================================================

set -e  # Berhenti jika ada error

# ── Konfigurasi — sesuaikan jika perlu ───────────────────────────────────────
APP_DIR="/root/bag-counter"
INSTALLER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

DB_NAME="bag_counter"
DB_USER="operator"
DB_PASS="operator123"
DB_ROOT_PASS=""        # kosongkan jika MariaDB root tanpa password

ADMIN_USER="admin"
ADMIN_PASS="admin123"

APP_PORT=5000
SERVICE_NAME="bag-counter"
# ─────────────────────────────────────────────────────────────────────────────

# Warna terminal
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

info()    { echo -e "${CYAN}[INFO]${NC}  $1"; }
success() { echo -e "${GREEN}[OK]${NC}    $1"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $1"; }
error()   { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }
section() {
    echo -e "\n${BOLD}══════════════════════════════════════════${NC}"
    echo -e "${BOLD}  $1${NC}"
    echo -e "${BOLD}══════════════════════════════════════════${NC}"
}

# ── Cek root ─────────────────────────────────────────────────────────────────
[[ $EUID -ne 0 ]] && error "Jalankan sebagai root: sudo bash install.sh"

# ── Cek file aplikasi tersedia ───────────────────────────────────────────────
[[ ! -f "$INSTALLER_DIR/app.py" ]] && error "app.py tidak ditemukan di $INSTALLER_DIR. Pastikan folder installer lengkap."

# =============================================================================
section "1/8 — Update System & Install Dependencies"
# =============================================================================
info "Update apt dan install dependensi sistem..."
apt-get update -qq
apt-get install -y -qq \
    python3 python3-pip python3-venv python3-dev \
    mariadb-server mariadb-client \
    libmariadb-dev pkg-config \
    git curl wget rsync \
    libgl1-mesa-glx libglib2.0-0 \
    build-essential cmake

success "Dependensi sistem terinstall"

# =============================================================================
section "2/8 — Setup MariaDB"
# =============================================================================
systemctl enable mariadb --quiet
systemctl start mariadb
sleep 2

mysql_exec() {
    if [[ -z "$DB_ROOT_PASS" ]]; then
        mysql -u root -e "$1" 2>/dev/null
    else
        mysql -u root -p"$DB_ROOT_PASS" -e "$1" 2>/dev/null
    fi
}

info "Membuat database '$DB_NAME'..."
mysql_exec "CREATE DATABASE IF NOT EXISTS \`$DB_NAME\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"

info "Membuat user '$DB_USER'..."
mysql_exec "CREATE USER IF NOT EXISTS '$DB_USER'@'localhost' IDENTIFIED BY '$DB_PASS';"
mysql_exec "GRANT ALL PRIVILEGES ON \`$DB_NAME\`.* TO '$DB_USER'@'localhost';"
mysql_exec "FLUSH PRIVILEGES;"

info "Membuat tabel user dan scan_history..."
mysql -u "$DB_USER" -p"$DB_PASS" "$DB_NAME" <<'SQL'
CREATE TABLE IF NOT EXISTS `user` (
    id           INT AUTO_INCREMENT PRIMARY KEY,
    username     VARCHAR(100) NOT NULL UNIQUE,
    password_hash VARCHAR(256) NOT NULL,
    role         VARCHAR(20) DEFAULT 'operator'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS `scan_history` (
    id           INT AUTO_INCREMENT PRIMARY KEY,
    start_time   DATETIME,
    end_time     DATETIME,
    total_bags   INT DEFAULT 0,
    bags_in      INT DEFAULT 0,
    bags_out     INT DEFAULT 0,
    video_source VARCHAR(100),
    model_name   VARCHAR(50),
    status       VARCHAR(20) DEFAULT 'completed'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
SQL

success "Database dan tabel siap"

# =============================================================================
section "3/8 — Salin File Aplikasi"
# =============================================================================
info "Menyalin file dari $INSTALLER_DIR ke $APP_DIR..."
mkdir -p "$APP_DIR/static/uploads"

# Salin semua file kecuali venv, log, patch script
rsync -a \
    --exclude='venv/' \
    --exclude='*.log' \
    --exclude='patch_*.py' \
    --exclude='seed_dummy.py' \
    --exclude='__pycache__/' \
    --exclude='install.sh' \
    "$INSTALLER_DIR/" "$APP_DIR/"

# Pastikan folder uploads tetap ada setelah sync
mkdir -p "$APP_DIR/static/uploads"
chmod 755 "$APP_DIR/static/uploads"

success "File aplikasi disalin ke $APP_DIR"

# =============================================================================
section "4/8 — Setup Python Virtual Environment"
# =============================================================================
VENV_DIR="$APP_DIR/venv"

info "Membuat virtual environment di $VENV_DIR..."
python3 -m venv "$VENV_DIR"

info "Upgrade pip..."
"$VENV_DIR/bin/pip" install --upgrade pip -q

info "Install Python dependencies (ini bisa memakan waktu beberapa menit)..."
"$VENV_DIR/bin/pip" install -q \
    "ultralytics>=8.0.0" \
    "opencv-python-headless>=4.8.0" \
    "flask>=3.0.0" \
    "flask-login>=0.6.0" \
    "flask-sqlalchemy>=3.0.0" \
    "pymysql>=1.1.0" \
    "pandas>=2.0.0" \
    "Pillow>=10.0.0" \
    "numpy>=1.24.0" \
    "werkzeug>=3.0.0"

success "Virtual environment siap"

# =============================================================================
section "5/8 — Konfigurasi Aplikasi"
# =============================================================================
DB_URI="mysql+pymysql://${DB_USER}:${DB_PASS}@localhost/${DB_NAME}"
APP_PY="$APP_DIR/app.py"

info "Update database URI di app.py..."
# Ganti baris SQLALCHEMY_DATABASE_URI dengan konfigurasi baru
sed -i "s|app.config\['SQLALCHEMY_DATABASE_URI'\].*|app.config['SQLALCHEMY_DATABASE_URI'] = '$DB_URI'|" "$APP_PY"

success "Konfigurasi database diupdate"

# =============================================================================
section "6/8 — Seed Admin User"
# =============================================================================
info "Membuat admin user '$ADMIN_USER'..."

"$VENV_DIR/bin/python3" - <<PYEOF
import sys, os
sys.path.insert(0, '$APP_DIR')
os.chdir('$APP_DIR')
from app import app, db, User
from werkzeug.security import generate_password_hash
with app.app_context():
    db.create_all()
    existing = User.query.filter_by(username='$ADMIN_USER').first()
    if not existing:
        u = User(
            username='$ADMIN_USER',
            password_hash=generate_password_hash('$ADMIN_PASS'),
            role='admin'
        )
        db.session.add(u)
        db.session.commit()
        print('[OK] Admin user berhasil dibuat')
    else:
        print('[SKIP] Admin user sudah ada, lewati')
PYEOF

success "Admin user siap"

# =============================================================================
section "7/8 — Setup Systemd Service (Auto-Start)"
# =============================================================================
info "Membuat service file /etc/systemd/system/${SERVICE_NAME}.service..."

cat > /etc/systemd/system/${SERVICE_NAME}.service <<EOF
[Unit]
Description=Bag Counter LLM — Computer Vision Dashboard
After=network.target mariadb.service
Requires=mariadb.service

[Service]
Type=simple
User=root
WorkingDirectory=${APP_DIR}
ExecStart=${VENV_DIR}/bin/python3 ${APP_DIR}/app.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "$SERVICE_NAME" --quiet
systemctl restart "$SERVICE_NAME"
sleep 3

if systemctl is-active --quiet "$SERVICE_NAME"; then
    success "Service '$SERVICE_NAME' aktif dan berjalan"
else
    warn "Service belum aktif. Cek log: journalctl -u $SERVICE_NAME -n 50 --no-pager"
fi

# =============================================================================
section "8/8 — Konfigurasi Firewall"
# =============================================================================
if command -v ufw &>/dev/null; then
    ufw allow "$APP_PORT/tcp" --comment "Bag Counter Dashboard" 2>/dev/null || true
    success "Port $APP_PORT dibuka di UFW"
else
    warn "UFW tidak ditemukan, lewati konfigurasi firewall"
    info "Buka port manual jika diperlukan: iptables -A INPUT -p tcp --dport $APP_PORT -j ACCEPT"
fi

# =============================================================================
# ── Ringkasan Instalasi ───────────────────────────────────────────────────────
# =============================================================================
IP_ADDR=$(hostname -I | awk '{print $1}')

echo ""
echo -e "${GREEN}╔══════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║      INSTALASI BERHASIL ✓                        ║${NC}"
echo -e "${GREEN}╠══════════════════════════════════════════════════╣${NC}"
echo -e "${GREEN}║${NC}  Dashboard  : ${BOLD}http://${IP_ADDR}:${APP_PORT}${NC}"
echo -e "${GREEN}║${NC}  Admin User : ${BOLD}${ADMIN_USER}${NC}"
echo -e "${GREEN}║${NC}  Admin Pass : ${BOLD}${ADMIN_PASS}${NC}"
echo -e "${GREEN}║${NC}  Database   : ${BOLD}${DB_NAME}${NC} (user: ${DB_USER})"
echo -e "${GREEN}║${NC}  App Dir    : ${BOLD}${APP_DIR}${NC}"
echo -e "${GREEN}║${NC}  Model AI   : ${BOLD}${APP_DIR}/yolov8n.pt${NC}"
echo -e "${GREEN}╠══════════════════════════════════════════════════╣${NC}"
echo -e "${GREEN}║${NC}  Status     : systemctl status ${SERVICE_NAME}"
echo -e "${GREEN}║${NC}  Log live   : journalctl -u ${SERVICE_NAME} -f"
echo -e "${GREEN}║${NC}  Restart    : systemctl restart ${SERVICE_NAME}"
echo -e "${GREEN}║${NC}  Stop       : systemctl stop ${SERVICE_NAME}"
echo -e "${GREEN}╚══════════════════════════════════════════════════╝${NC}"
echo ""
