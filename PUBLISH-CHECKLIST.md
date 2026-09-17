# 开源发布前的检查清单

这个包已经可以正常运行，但在推到 GitHub 之前，请把下面这些**占位内容**改成你自己的。

## 必改

- [ ] **LICENSE**：把 `Copyright (c) 2026 voice-tts-system contributors`
      改成你的名字或组织名。

- [ ] **client/astrbot_plugin_voice_reply/metadata.yaml**：

      ```yaml
      author: rvc-tts                                  # ← 改成你的 GitHub 用户名
      repo: https://github.com/your-name/astrbot_plugin_voice_reply   # ← 改成真实仓库地址
      ```
      如果打算把插件作为独立仓库发布，`repo` 要指向那个仓库。

- [ ] **README.md 顶部**：如果项目改名了，把标题和描述一起改掉。

- [ ] **server/config.yaml**、**deploy/linux/config.yaml**：
      把 `security.api_key` 换成你自己的密钥，
      **不要**把你现在用的密钥直接提交上去。

- [ ] 搜一遍有没有泄露的隐私信息：

      ```bash
      grep -rn "api_key\|password\|192\.168\.\|10\.0\.\|/root/\|/opt/tts-server" \
        --include="*.yaml" --include="*.md" --include="*.py" .
      ```

## 建议

- [ ] 确认你**有权分发**所使用的 RVC 模型文件；
      仓库里**不要**附带任何 `.pth` / `.index` 模型。

- [ ] `models/` 目录只保留 `.gitkeep`，`output/` 同理。

- [ ] 检查依赖许可证是否与 MIT 兼容：`praat-parselmouth` 与 `espeak-ng` 是 GPL，
      如果你只是调用它们（不修改、不静态链接分发），一般没问题；
      但如果你把它们的二进制打包进发布物，需要一并提供对应源码或声明。

- [ ] 加上 CI（可选）：仓库里 `tests/run_all.py` 可以一条命令跑完全部自测，
      直接接 GitHub Actions 即可。

      ```yaml
      # .github/workflows/test.yml
      name: tests
      on: [push, pull_request]
      jobs:
        test:
          runs-on: ubuntu-latest
          steps:
            - uses: actions/checkout@v4
            - uses: actions/setup-python@v5
              with: { python-version: "3.12" }
            - run: pip install fastapi uvicorn pydantic pyyaml httpx
            - run: python tests/run_all.py
      ```

- [ ] 仓库体积：**不要把 Windows 整合包（3.5GB）和模型权重提交进 Git**。
      大文件放到 GitHub Release 的 Assets 里，仓库里只留构建脚本。
      `.gitignore` 已经排除了 `dist/`、`models/`、`.build-cache/`。

- [ ] 在 README 里加一张网页控制台的截图，会显著提升项目观感。

## 打包产物怎么发

建议这样发版本：

| 内容 | 放哪 |
| --- | --- |
| 源码 + 文档 + 部署脚本（本包） | GitHub 仓库 |
| `voice-tts-system-v1.0.0.zip` | GitHub Release Assets |
| Windows 整合包（3.5GB） | GitHub Release Assets（或对象存储） |
| `astrbot-plugin-voice-reply-v1.0.0.zip` | GitHub Release Assets |
| 安卓 APK | GitHub Release Assets |

> GitHub Release 单个文件上限 2GB，Windows 整合包超了就放对象存储，
> 或者拆成「整合包骨架 + 依赖下载脚本」。
