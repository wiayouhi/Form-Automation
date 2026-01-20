import time, random, threading, requests, json
import psutil  # <--- ส่วนที่เพิ่มมา: สำหรับดึงค่า CPU/RAM
from queue import Queue
from flask import Flask, render_template, request, Response, jsonify
from bs4 import BeautifulSoup
from urllib.parse import urlparse, parse_qs

app = Flask(__name__)

# Global resources
count_lock = threading.Lock()
job_queue = Queue()
log_queue = Queue()
stop_event = threading.Event()

SUCCESS_COUNT = [0]
FAIL_COUNT = [0]
TOTAL_JOBS = 0

# =========================
# Utils
# =========================
def fetch_fbzx(view_url):
    try:
        r = requests.get(view_url, timeout=5)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        tag = soup.find("input", {"name": "fbzx"})
        return tag["value"] if tag else None
    except:
        return None

# ค้นหาฟังก์ชัน submission_worker แล้วแทนที่ด้วย code นี้
def submission_worker(post_url, fbzx, page_history, entries_map, mode, delay):
    while not stop_event.is_set():
        try:
            idx = job_queue.get(timeout=1)
        except:
            if job_queue.empty(): break
            continue

        if stop_event.is_set():
            job_queue.task_done()
            break

        if stop_event.wait(delay): 
            job_queue.task_done()
            break

        payload = {
            "fvv": "1", 
            "fbzx": fbzx, 
            "pageHistory": page_history,
            "draftResponse": [],
            "partialResponse": "[null,null,\"" + fbzx + "\"]" 
        }
        
        # --- ส่วนที่แก้ไข Logic การเลือกคำตอบ ---
        for eid, data in entries_map.items():
            # รับข้อมูลเป็น dict: {'values': 'A,B,C', 'unique': True/False}
            raw_val = data.get('values', '')
            is_unique = data.get('unique', False)
            
            options = [v.strip() for v in raw_val.split(',') if v.strip()]
            if not options: continue
            
            if is_unique:
                # โหมดห้ามซ้ำ: เลือกตามลำดับงาน (Job ID)
                # ถ้าจำนวนงานเกินคำตอบที่มี จะวนกลับมาใช้ตัวแรก (Modulo)
                answer = options[idx % len(options)]
            else:
                # โหมดปกติ: สุ่ม หรือ เรียงตาม Mode หลัก
                if mode == 'R':
                    answer = random.choice(options)
                else:
                    answer = options[idx % len(options)]
            
            payload[eid] = answer
        # -------------------------------------

        try:
            r = requests.post(post_url, data=payload, timeout=10)
            if r.status_code in (200, 302, 303):
                status = "SUCCESS"
                with count_lock: SUCCESS_COUNT[0] += 1
            else:
                status = f"FAIL({r.status_code})"
                with count_lock: FAIL_COUNT[0] += 1
        except Exception as e:
            status = "ERROR"
            with count_lock: FAIL_COUNT[0] += 1
        
        log_queue.put(f"[{time.strftime('%H:%M:%S')}] #{idx+1} | {status}")
        job_queue.task_done()

        
@app.route('/ping', methods=['GET'])
def ping():
    return jsonify({"status": "ok", "timestamp": time.time()})

# --- ส่วนที่เพิ่มมา: API สำหรับดึง CPU/RAM ---
@app.route('/api/stats', methods=['GET'])
def server_stats():
    try:
        # interval=None จะไม่บล็อกการทำงาน (non-blocking) 
        # แต่มันจะเทียบค่าจากครั้งล่าสุดที่เรียก ซึ่งเหมาะสำหรับ Realtime UI
        cpu = psutil.cpu_percent(interval=None)
        ram = psutil.virtual_memory().percent
        return jsonify({
            "cpu": cpu,
            "ram": ram
        })
    except Exception as e:
        return jsonify({"cpu": 0, "ram": 0})
# ----------------------------------------

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/stop', methods=['POST'])
def stop_process():
    """API สำหรับสั่งหยุดการทำงานทันที"""
    stop_event.set()
    with job_queue.mutex:
        job_queue.queue.clear()
    log_queue.put("🛑 STOP SIGNAL RECEIVED. HALTING PROCESS...")
    return jsonify({"message": "Stopping process..."})

@app.route('/api/parse-url', methods=['POST'])
def parse_url():
    url = request.json.get('url', '')
    try:
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        entries = {k: v[0] for k, v in params.items() if k.startswith("entry.")}
        return jsonify({"entries": entries})
    except:
        return jsonify({"entries": {}})

@app.route('/api/stream')
def stream():
    global job_queue, SUCCESS_COUNT, FAIL_COUNT, TOTAL_JOBS
    
    # Reset State
    stop_event.clear()
    with job_queue.mutex: job_queue.queue.clear()
    with log_queue.mutex: log_queue.queue.clear()
    SUCCESS_COUNT[0] = 0
    FAIL_COUNT[0] = 0

    # Params
    form_url = request.args.get('url')
    TOTAL_JOBS = int(request.args.get('total', 1))
    bots = int(request.args.get('bots', 1))
    delay = float(request.args.get('delay', 0.5))
    mode = request.args.get('mode', 'R')
    num_pages = int(request.args.get('pages', 1)) 
    entries_map = json.loads(request.args.get('entries'))

    # URL Logic
    base = form_url.split("?")[0].rstrip("/")
    if base.endswith(("viewform", "formResponse")):
        base = base.rsplit("/", 1)[0]
    post_url = f"{base}/formResponse"
    view_url = f"{base}/viewform"
    
    # Fetch Token
    fbzx = fetch_fbzx(view_url)
    if not fbzx:
        return Response("data: ❌ Error: Cannot fetch fbzx token. Is the form public?\n\n", mimetype='text/event-stream')

    page_history = ",".join(str(i) for i in range(num_pages))
    
    # Fill Queue
    for i in range(TOTAL_JOBS):
        job_queue.put(i)
    
    # Start Threads
    threads = []
    for _ in range(bots):
        t = threading.Thread(
            target=submission_worker,
            args=(post_url, fbzx, page_history, entries_map, mode, delay)
        )
        t.daemon = True 
        t.start()
        threads.append(t)

    def process_generator():
        yield f"data: 🚀 Started: {TOTAL_JOBS} Jobs on {num_pages} Pages Form.\n\n"
        
        while any(t.is_alive() for t in threads) or not log_queue.empty():
            if stop_event.is_set() and job_queue.empty() and log_queue.empty():
                break

            while not log_queue.empty():
                yield f"data: {log_queue.get()}\n\n"
            
            if SUCCESS_COUNT[0] + FAIL_COUNT[0] >= TOTAL_JOBS:
                pass 
                
            time.sleep(0.1)

        while not log_queue.empty():
            yield f"data: {log_queue.get()}\n\n"

        if stop_event.is_set():
             yield "data: 🛑 Process Stopped by User.\n\n"
        else:
             yield f"data: 🏁 FINISHED! Success: {SUCCESS_COUNT[0]} | Fail: {FAIL_COUNT[0]}\n\n"

    return Response(process_generator(), mimetype='text/event-stream')

if __name__ == '__main__':
    # เรียกครั้งแรกเพื่อให้ psutil เริ่มนับ cycle cpu
    try:
        psutil.cpu_percent()
    except:
        pass
    app.run(debug=True, threaded=True, host='0.0.0.0', port=5000)
