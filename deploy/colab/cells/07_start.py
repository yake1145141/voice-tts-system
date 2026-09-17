import json, os, subprocess, sys, time, urllib.request

os.makedirs(f"{WORKDIR}/logs", exist_ok=True)
os.makedirs(f"{WORKDIR}/output", exist_ok=True)
log_file = open(f"{WORKDIR}/logs/server.log", "w", encoding="utf-8")
env = {**os.environ, "PYTHONUNBUFFERED": "1", "TMPDIR": "/tmp"}

server = subprocess.Popen(
    [sys.executable, "-s", f"{WORKDIR}/app/main.py", "--config", f"{WORKDIR}/config.yaml"],
    cwd=WORKDIR, stdout=log_file, stderr=subprocess.STDOUT, env=env)
print("服务进程 PID:", server.pid, "（首次要加载模型，稍等几十秒）")


def health():
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/health", timeout=5) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


ready = False
for _ in range(150):
    if server.poll() is not None:
        break
    info = health()
    if info and info.get("success"):
        eng = info["engine"]
        name = os.path.basename(eng.get("model") or "") or "-"
        print(f"✅ 服务就绪：device={eng['device']} is_half={eng['is_half']} "
              f"model={name} f0={eng['f0_method']}")
        ready = True
        break
    time.sleep(2)

if not ready:
    print("❌ 启动失败，日志尾部：")
    with open(f"{WORKDIR}/logs/server.log", encoding="utf-8", errors="ignore") as fh:
        print("".join(fh.readlines()[-30:]))
