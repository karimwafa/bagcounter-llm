import os
import cv2
import threading
import json
import time
import pandas as pd
from datetime import datetime, timedelta
from flask import Flask, render_template, Response, request, jsonify, send_from_directory, redirect, url_for
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps

app = Flask(__name__)
app.config['SECRET_KEY'] = 'dev-key-123'
app.config['SQLALCHEMY_DATABASE_URI'] = 'mysql+pymysql://operator:operator123@localhost/bag_counter'
app.config['UPLOAD_FOLDER'] = os.path.join(os.getcwd(), 'static/uploads')
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'

# Models
class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(100), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    role = db.Column(db.String(20), default='operator') # 'admin' or 'operator'

class ScanHistory(db.Model):
    __tablename__ = 'scan_history'
    id = db.Column(db.Integer, primary_key=True)
    start_time = db.Column(db.DateTime, default=datetime.utcnow)
    end_time = db.Column(db.DateTime)
    video_source = db.Column(db.String(100))
    model_name = db.Column(db.String(50))
    total_bags = db.Column(db.Integer, default=0)
    bags_in = db.Column(db.Integer, default=0)
    bags_out = db.Column(db.Integer, default=0)
    status = db.Column(db.String(20), default='completed')

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or current_user.role != 'admin':
            return jsonify({'ok': False, 'msg': 'Admin access required'}), 403
        return f(*args, **kwargs)
    return decorated_function

# ─────────────────────────────────────────────
#  Real YOLOv8 Counter Engine
# ─────────────────────────────────────────────
class CounterEngine:
    def __init__(self):
        self.is_running = False
        self.total = 0
        self.in_c = 0
        self.out_c = 0
        self.current_session_id = None
        self.current_frame = None       # JPEG bytes untuk streaming MJPEG
        self.lock = threading.Lock()
        self._last_db_update = time.time()

    def start(self, source, weights):
        if self.is_running:
            return False
        self.source = source
        self.weights = weights
        self.total = 0
        self.in_c = 0
        self.out_c = 0
        self.current_frame = None

        # Validasi source bisa dibuka sebelum mulai thread
        try:
            src = int(source) if str(source).isdigit() else source
            cap_test = cv2.VideoCapture(src)
            if not cap_test.isOpened():
                cap_test.release()
                return False
            cap_test.release()
        except Exception:
            return False

        # Simpan sesi awal ke DB
        with app.app_context():
            hist = ScanHistory(video_source=str(source), model_name=weights, status='running')
            db.session.add(hist)
            db.session.commit()
            self.current_session_id = hist.id

        self.is_running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        return True

    def _run(self):
        """
        Loop utama deteksi:
        1. Buka video/kamera via OpenCV
        2. Jalankan YOLOv8 tracking per frame
        3. Hitung crossing garis virtual horizontal tengah frame (IN/OUT)
        4. Encode frame → simpan ke self.current_frame untuk MJPEG stream
        5. Setiap 30 detik update DB secara berkala
        """
        from ultralytics import YOLO

        try:
            model = YOLO(self.weights)
        except Exception as e:
            print(f"[ENGINE] Gagal load model: {e}")
            self._finish_session()
            return

        src = int(self.source) if str(self.source).isdigit() else self.source
        cap = cv2.VideoCapture(src)

        if not cap.isOpened():
            print(f"[ENGINE] Tidak bisa buka source: {self.source}")
            self._finish_session()
            return

        frame_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        frame_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        LINE_Y  = frame_h // 2     # garis virtual horizontal di tengah
        TOLERANCE = 10              # toleransi piksel agar tidak double-count

        tracked = {}               # {track_id: last_center_y}

        print(f"[ENGINE] Started — source={self.source}, model={self.weights}, line_y={LINE_Y}")

        while self.is_running:
            ret, frame = cap.read()
            if not ret:
                if str(self.source).isdigit():
                    break               # kamera putus → stop
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)   # video file → loop ulang
                continue

            # YOLOv8 tracking
            results = model.track(frame, persist=True, verbose=False, conf=0.35, iou=0.45)
            annotated = results[0].plot()

            boxes = results[0].boxes
            if boxes is not None and boxes.id is not None:
                ids   = boxes.id.cpu().numpy().astype(int)
                xyxys = boxes.xyxy.cpu().numpy()

                for tid, xyxy in zip(ids, xyxys):
                    cy = int((xyxy[1] + xyxy[3]) / 2)   # center Y bounding box

                    if tid in tracked:
                        prev_y = tracked[tid]
                        # Crossing ke bawah → IN
                        if prev_y < LINE_Y - TOLERANCE and cy >= LINE_Y + TOLERANCE:
                            self.in_c  += 1
                            self.total += 1
                        # Crossing ke atas → OUT
                        elif prev_y > LINE_Y + TOLERANCE and cy <= LINE_Y - TOLERANCE:
                            self.out_c += 1
                            self.total += 1

                    tracked[tid] = cy

            # Gambar garis virtual & overlay counter
            cv2.line(annotated, (0, LINE_Y), (frame_w, LINE_Y), (0, 255, 255), 2)
            cv2.putText(annotated, f"IN : {self.in_c}",  (10, 40),  cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 255, 100), 2)
            cv2.putText(annotated, f"OUT: {self.out_c}", (10, 80),  cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 80, 255),  2)
            cv2.putText(annotated, f"TOT: {self.total}", (10, 120), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (255, 255, 255), 2)

            # Encode → simpan untuk streaming
            ok, buf = cv2.imencode('.jpg', annotated, [cv2.IMWRITE_JPEG_QUALITY, 75])
            if ok:
                with self.lock:
                    self.current_frame = buf.tobytes()

            # Update DB setiap 30 detik
            now = time.time()
            if now - self._last_db_update >= 30:
                self._update_db_live()
                self._last_db_update = now

        cap.release()
        self._finish_session()

    def _update_db_live(self):
        """Update hitungan ke DB saat engine masih berjalan (live update)."""
        if not self.current_session_id:
            return
        try:
            with app.app_context():
                hist = ScanHistory.query.get(self.current_session_id)
                if hist:
                    hist.total_bags = self.total
                    hist.bags_in    = self.in_c
                    hist.bags_out   = self.out_c
                    db.session.commit()
                    print(f"[ENGINE] Live DB update — total={self.total}, in={self.in_c}, out={self.out_c}")
        except Exception as e:
            print(f"[ENGINE] DB live update error: {e}")

    def _finish_session(self):
        """Simpan hasil akhir dan tandai sesi selesai."""
        self.is_running = False
        if not self.current_session_id:
            return
        try:
            with app.app_context():
                hist = ScanHistory.query.get(self.current_session_id)
                if hist:
                    hist.end_time   = datetime.utcnow()
                    hist.total_bags = self.total
                    hist.bags_in    = self.in_c
                    hist.bags_out   = self.out_c
                    hist.status     = 'completed'
                    db.session.commit()
                    print(f"[ENGINE] Sesi #{self.current_session_id} selesai — total={self.total}, in={self.in_c}, out={self.out_c}")
        except Exception as e:
            print(f"[ENGINE] DB finish error: {e}")

    def stop(self):
        self.is_running = False
        time.sleep(0.5)
        self._finish_session()

    def generate_frames(self):
        """Generator MJPEG untuk route /video_feed."""
        while self.is_running:
            with self.lock:
                frame = self.current_frame
            if frame:
                yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + frame + b'\r\n')
            time.sleep(0.033)   # ~30 fps

engine = CounterEngine()


# Routes
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        u = User.query.filter_by(username=username).first()
        if u and check_password_hash(u.password_hash, password):
            login_user(u)
            return redirect(url_for('index'))
        else:
            from flask import flash
            flash('Username atau Password salah!')
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

@app.route('/')
@login_required
def index():
    return render_template('index.html')

@app.route('/admin')
@admin_required
def admin():
    return render_template('admin.html')

@app.route('/api/start', methods=['POST'])
@login_required
def api_start():
    data = request.json
    if engine.start(data['source'], data['weights']): return jsonify({'ok': True})
    return jsonify({'ok': False, 'msg': 'Already running'})

@app.route('/api/stop', methods=['POST'])
@login_required
def api_stop():
    engine.stop()
    return jsonify({'ok': True})

@app.route('/api/stats')
@login_required
def api_stats():
    return jsonify({'total': engine.total, 'in': engine.in_c, 'out': engine.out_c})

@app.route('/api/upload', methods=['POST'])
@login_required
def api_upload():
    if 'video' not in request.files: return jsonify({'ok': False})
    file = request.files['video']
    path = os.path.join(app.config['UPLOAD_FOLDER'], file.filename)
    file.save(path)
    return jsonify({'ok': True, 'path': path})

@app.route('/api/admin/stats')
@admin_required
def admin_stats():
    now = datetime.utcnow()
    day = now - timedelta(days=1)
    week = now - timedelta(days=7)
    month = now - timedelta(days=30)
    return jsonify({
        'total': db.session.query(db.func.sum(ScanHistory.total_bags)).scalar() or 0,
        'daily': db.session.query(db.func.sum(ScanHistory.total_bags)).filter(ScanHistory.start_time >= day).scalar() or 0,
        'weekly': db.session.query(db.func.sum(ScanHistory.total_bags)).filter(ScanHistory.start_time >= week).scalar() or 0,
        'monthly': db.session.query(db.func.sum(ScanHistory.total_bags)).filter(ScanHistory.start_time >= month).scalar() or 0,
    })

@app.route('/api/admin/chart-data')
@admin_required
def admin_chart():
    now = datetime.utcnow().replace(minute=0, second=0, microsecond=0)
    labels = []; data = []
    for i in range(23, -1, -1):
        h_start = now - timedelta(hours=i)
        h_end = h_start + timedelta(hours=1)
        count = db.session.query(db.func.sum(ScanHistory.total_bags)).filter(ScanHistory.start_time >= h_start, ScanHistory.start_time < h_end).scalar() or 0
        labels.append(h_start.strftime('%H:00'))
        data.append(int(count))
    return jsonify({'labels': labels, 'data': data})

@app.route('/api/history')
@login_required
def manage_history():
    start_date = request.args.get('start')
    end_date = request.args.get('end')
    page = int(request.args.get('page', 1))
    per_page = int(request.args.get('per_page', 10))

    query = ScanHistory.query
    
    if not start_date or start_date == 'null' or start_date == '':
        start_date_dt = datetime.utcnow() - timedelta(days=10)
        start_date = start_date_dt.strftime('%Y-%m-%d')
    
    if not end_date or end_date == 'null' or end_date == '':
        end_date = datetime.utcnow().strftime('%Y-%m-%d')

    query = query.filter(ScanHistory.start_time >= start_date + ' 00:00:00')
    query = query.filter(ScanHistory.start_time <= end_date + ' 23:59:59')
    
    pagination = query.order_by(ScanHistory.start_time.desc()).paginate(page=page, per_page=per_page, error_out=False)
    history = pagination.items
    
    res_data = [{'id': h.id, 'start': h.start_time.strftime('%Y-%m-%d %H:%M'), 'source': h.video_source or '-', 'total': h.total_bags, 'in': h.bags_in, 'out': h.bags_out} for h in history]
    
    # Summary for the filtered range (all pages)
    all_filtered = query.all()
    
    return jsonify({
        'data': res_data,
        'page': pagination.page,
        'pages': pagination.pages,
        'total_items': pagination.total,
        'summary': {
            'total': sum(h.total_bags for h in all_filtered),
            'in': sum(h.bags_in for h in all_filtered),
            'out': sum(h.bags_out for h in all_filtered)
        }
    })

@app.route('/api/admin/export')
@admin_required
def admin_export():
    history = ScanHistory.query.order_by(ScanHistory.start_time.desc()).all()
    df = pd.DataFrame([{'Time': h.start_time, 'Source': h.video_source, 'Model': h.model_name, 'Total': h.total_bags, 'In': h.bags_in, 'Out': h.bags_out, 'Status': h.status} for h in history])
    csv_path = os.path.join(app.config['UPLOAD_FOLDER'], 'report.csv')
    df.to_csv(csv_path, index=False)
    return send_from_directory(app.config['UPLOAD_FOLDER'], 'report.csv', as_attachment=True)

@app.route('/api/users', methods=['GET', 'POST'])
@admin_required
def manage_users():
    if request.method == 'POST':
        data = request.json
        if User.query.filter_by(username=data['username']).first(): return jsonify({'ok': False, 'msg': 'User exists'})
        u = User(username=data['username'], password_hash=generate_password_hash(data['password']), role=data['role'])
        db.session.add(u); db.session.commit()
        return jsonify({'ok': True})
    users = User.query.all()
    return jsonify([{'id': u.id, 'username': u.username, 'role': u.role} for u in users])

@app.route('/api/users/<int:uid>', methods=['PUT', 'DELETE'])
@admin_required
def edit_user(uid):
    u = User.query.get(uid)
    if not u: return jsonify({'ok': False})
    if request.method == 'DELETE':
        if u.username == 'admin': return jsonify({'ok': False})
        db.session.delete(u); db.session.commit()
        return jsonify({'ok': True})
    data = request.json
    if data.get('password'): u.password_hash = generate_password_hash(data['password'])
    if u.username != 'admin': u.role = data['role']
    db.session.commit()
    return jsonify({'ok': True})

@app.route('/video_feed')
@login_required
def video_feed():
    return Response(engine.generate_frames(), mimetype='multipart/x-mixed-replace; boundary=frame')


def init_db():
    with app.app_context():
        db.create_all()
        if not User.query.filter_by(username='admin').first():
            admin = User(username='admin', password_hash=generate_password_hash('admin123'), role='admin')
            db.session.add(admin); db.session.commit()

if __name__ == '__main__':
    init_db()
    app.run(host='0.0.0.0', port=5000)