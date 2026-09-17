# 停止服务与隧道（想重新开始就从头再运行一遍）
import subprocess

for pattern in ("cloudflared", "app/main.py"):
    subprocess.run(f"pkill -f '{pattern}'", shell=True)
print("已停止")
