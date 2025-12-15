import time, random, threading, requests
import json
from queue import Queue
from flask import Flask, render_template, request, Response, jsonify
from bs4 import BeautifulSoup
from urllib.parse import urlparse, parse_qs
from requests.exceptions import RequestException

app = Flask(__name__)

# Global resources for thread management
count_lock = threading.Lock()
job_queue = Queue()
log_queue = Queue()
stop_event = threading.Event()

# ตัวแปรสำหรับนับผลลัพธ์
SUCCESS_COUNT = [0]
FAIL_COUNT = [0]
TOTAL_JOBS = 0

# =========================
# Utils & Core Logic
# =========================

def fetch_fbzx(view_url):
    """ดึงค่า fbzx จากหน้า viewform"""
    try:
        r = requests.get(view_url, timeout=5)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        tag = soup.find("input", {"name": "fbzx"})
        return tag["value"] if tag else None
    except RequestException as e:
        # log_queue.put(f"❌ ERROR: Cannot fetch fbzx - {e}")
        return None

def submission_worker(post_url, fbzx, page_history, entries_map, mode, delay):
    """ฟังก์ชัน Worker ที่รันในแต่ละ Thread เพื่อส่งฟอร์ม"""
    while not job_queue.empty() and not stop_event.is_set():
        try:
            # ดึงลำดับงานจาก Queue
            idx = job_queue.get(timeout=1)
        except:
            continue # Queue empty

        time.sleep(delay) # หน่วงเวลาตามที่ตั้งค่า

        # 1. สร้าง Payload
        payload = {
            "fvv": "1", 
            "fbzx": fbzx, 
            "pageHistory": page_history,
            "draftResponse": []
        }
        
        for eid, val in entries_map.items():
            options = [v.strip() for v in val.split(',') if v.strip()]
            
            # การเลือกคำตอบ: Random หรือ Sequential
            if mode == 'R':
                answer = random.choice(options)
            else:
                # ใช้ idx ในการวนคำตอบแบบ Sequential
                answer = options[idx % len(options)] 
                
            payload[eid] = answer

        # 2. ส่ง Request
        try:
            r = requests.post(post_url, data=payload, timeout=10)
            if r.status_code in (200, 302, 303):
                status = f"✅ SUCCESS (HTTP {r.status_code})"
                with count_lock: SUCCESS_COUNT[0] += 1
            else:
                status = f"❌ FAIL (HTTP {r.status_code})"
                with count_lock: FAIL_COUNT[0] += 1
        except RequestException as e:
            status = f"⚠️ ERROR ({type(e).__name__})"
            with count_lock: FAIL_COUNT[0] += 1
        
        # ส่ง Log กลับไปที่ Log Queue
        log_queue.put(f"[{time.strftime('%H:%M:%S')}] #{idx+1} | {status} | Total: {SUCCESS_COUNT[0]}/{TOTAL_JOBS}")
        job_queue.task_done()

# =========================
# Flask Routes
# =========================

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/parse-url', methods=['POST'])
def parse_url():
    url = request.json.get('url', '')
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    entries = {k: v[0] for k, v in params.items() if k.startswith("entry.")}
    return jsonify({"entries": entries})

@app.route('/api/stream')
def stream():
    """API สำหรับเริ่มการส่งฟอร์มด้วย Thread และส่ง Log (SSE)"""
    global job_queue, SUCCESS_COUNT, FAIL_COUNT, TOTAL_JOBS
    
    # Reset สถานะและ Queue ทุกครั้งที่เริ่ม
    job_queue = Queue()
    log_queue.queue.clear() # Clear log queue
    SUCCESS_COUNT[0] = 0
    FAIL_COUNT[0] = 0
    stop_event.clear()

    # 1. รับค่า Configuration
    form_url = request.args.get('url')
    TOTAL_JOBS = int(request.args.get('total', 1))
    bots = int(request.args.get('bots', 1))
    delay = float(request.args.get('delay', 0.5))
    mode = request.args.get('mode', 'R')
    num_pages = int(request.args.get('pages', 1))
    
    entries_raw = request.args.get('entries')
    entries_map = json.loads(entries_raw)

    # 2. เตรียม URL และ fbzx
    base = form_url.split("?")[0].rstrip("/")
    if base.endswith(("viewform", "formResponse")):
        base = base.rsplit("/", 1)[0]
    post_url = f"{base}/formResponse"
    view_url = f"{base}/viewform"
    
    fbzx = fetch_fbzx(view_url)
    
    if not fbzx:
        def error_generator():
            yield "data: ❌ ERROR: Cannot fetch fbzx token or URL is invalid.\n\n"
        return Response(error_generator(), mimetype='text/event-stream')

    # 3. คำนวณ pageHistory
    page_history = ",".join(str(i) for i in range(num_pages))
    
    # 4. Populate Job Queue
    for i in range(TOTAL_JOBS):
        job_queue.put(i)
    
    # 5. Start Worker Threads
    threads = []
    for _ in range(bots):
        thread = threading.Thread(
            target=submission_worker,
            args=(post_url, fbzx, page_history, entries_map, mode, delay)
        )
        threads.append(thread)
        thread.start()

    # 6. Generator Function (Main thread for SSE)
    def process_generator():
        start_time = time.time()
        
        yield f"data: 🚀 Starting process with {TOTAL_JOBS} submissions, {bots} bots, {num_pages} sections (History: {page_history}).\n\n"
        
        while SUCCESS_COUNT[0] + FAIL_COUNT[0] < TOTAL_JOBS:
            if not log_queue.empty():
                log_msg = log_queue.get()
                yield f"data: {log_msg}\n\n"
            
            # ป้องกันการหน่วง Main thread มากเกินไป
            time.sleep(0.05) 

        # รอให้ Log สุดท้ายถูกส่ง (LogQueue อาจจะช้ากว่าการนับเล็กน้อย)
        while not log_queue.empty():
            log_msg = log_queue.get()
            yield f"data: {log_msg}\n\n"

        duration = time.time() - start_time
        yield f"data: 🏁 FINISHED! Total Time: {duration:.2f}s | Success: {SUCCESS_COUNT[0]} | Fail: {FAIL_COUNT[0]}\n\n"

        # Cleanup threads
        job_queue.join() 
        for t in threads:
            if t.is_alive():
                 t.join(timeout=0.1)


    return Response(process_generator(), mimetype='text/event-stream')


if __name__ == '__main__':
    app.run(debug=True, threaded=True)