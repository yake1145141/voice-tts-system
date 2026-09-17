# 查看服务端日志尾部（排错用）
with open(f"{WORKDIR}/logs/server.log", encoding="utf-8", errors="ignore") as fh:
    print("".join(fh.readlines()[-40:]))
