# 让这个单元格一直跑着：既能看到实时状态，也能让 Colab 认为会话在用
import json, time, urllib.request

while True:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/health", timeout=5) as resp:
            info = json.loads(resp.read())
        eng = info["engine"]
        stats = eng["stats"]
        print(f"[{time.strftime('%H:%M:%S')}] {info['status']} | {eng['device']} | "
              f"推理 {stats['total']} 次（成功 {stats['success']} / 失败 {stats['failed']}）| "
              f"最近 {stats.get('last_duration')}s", flush=True)
    except Exception as exc:
        print(f"[{time.strftime('%H:%M:%S')}] 服务不可用: {exc}", flush=True)
    time.sleep(60)
