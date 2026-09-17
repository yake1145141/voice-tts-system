import os, re, subprocess, time

CLOUDFLARED = "/usr/local/bin/cloudflared"
if not os.path.exists(CLOUDFLARED):
    print("下载 cloudflared …")
    subprocess.run(
        "wget -q https://github.com/cloudflare/cloudflared/releases/latest/download/"
        f"cloudflared-linux-amd64 -O {CLOUDFLARED} && chmod +x {CLOUDFLARED}",
        shell=True, check=True)

print("启动隧道 …")
tunnel_log_path = "/content/cloudflared.log"
tunnel_log = open(tunnel_log_path, "w", encoding="utf-8")
tunnel = subprocess.Popen(
    [CLOUDFLARED, "tunnel", "--url", f"http://127.0.0.1:{PORT}", "--no-autoupdate"],
    stdout=tunnel_log, stderr=subprocess.STDOUT)

public_url = None
for _ in range(60):
    time.sleep(1)
    text = open(tunnel_log_path, encoding="utf-8", errors="ignore").read()
    found = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", text)
    if found:
        public_url = found.group(0)
        break

if public_url:
    print("\n" + "=" * 68)
    print("公网地址（复制下面这段填进 AstrBot 插件配置）")
    print("=" * 68)
    print(f"""
tts_server:
  url: "{public_url}"
  api_key: "{API_KEY}"
  delivery: "file"

voice:
  enabled: true
  max_text_length: {MAX_TEXT_LENGTH}
  timeout: 90
  max_concurrent: 2
""")
    print("自检命令：")
    print(f'  curl -H "Authorization: Bearer {API_KEY}" {public_url}/api/health')
    print("=" * 68)
else:
    print("❌ 没拿到公网地址，隧道日志尾部：")
    print(open(tunnel_log_path, encoding="utf-8", errors="ignore").read()[-1500:])
