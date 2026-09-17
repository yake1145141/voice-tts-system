package com.yake.ttsserver;

import android.app.Activity;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.media.MediaPlayer;
import android.os.Bundle;
import android.text.InputType;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.widget.ArrayAdapter;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.Spinner;
import android.widget.TextView;
import android.widget.Toast;

import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.Base64;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * 安卓端语音服务配置 App。
 *
 * 作用：
 *  - 修改运行在 Ubuntu chroot 里的 tts-server 配置（端口 / API Key / 模型 / f0 / 音调 / 保留时间 / 并发 / 线程）
 *  - 启动 / 停止 / 重启服务，查看状态与日志
 *  - 直接测试合成一句话并播放
 *
 * 依赖：设备已 root（KernelSU/Magisk），服务端已按 deploy/android 的脚本装好。
 * 只用系统 API，不依赖 AndroidX，方便离线构建。
 */
public class MainActivity extends Activity {

    private static final String ROOTFS = "/data/tts-rootfs";
    private static final String TTS_DIR = ROOTFS + "/opt/tts-server";
    private static final String CONFIG = TTS_DIR + "/config.yaml";
    private static final String LOG = TTS_DIR + "/logs/server.log";
    private static final String[] CTL_CANDIDATES = {
            "/data/tts-ctl.sh", "/data/local/tmp/ttsctl.sh"
    };
    /** su 可执行文件候选路径（安卓 App 的 PATH 里通常没有 su，必须写绝对路径） */
    private static final String[] SU_CANDIDATES = {
            "/system/bin/su", "/system/xbin/su",
            "/data/adb/ksu/bin/su", "/data/adb/magisk/su", "su"
    };
    private static String suBinary = null;

    private EditText etPort, etKey, etModel, etIndex, etPitch, etExpire, etConc, etThreads, etText;
    private Spinner spF0, spFormat;
    private TextView tvStatus, tvLog;
    private SharedPreferences prefs;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        prefs = getSharedPreferences("tts", MODE_PRIVATE);

        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        int pad = dp(14);
        root.setPadding(pad, pad, pad, pad);

        root.addView(title("语音服务配置（手机本地 TTS + RVC）"));

        tvStatus = new TextView(this);
        tvStatus.setTextSize(13);
        tvStatus.setPadding(0, dp(6), 0, dp(10));
        tvStatus.setText("点「刷新状态」查看服务情况");
        root.addView(tvStatus);

        etPort = addRow(root, "端口", "8080", InputType.TYPE_CLASS_NUMBER);
        etKey = addRow(root, "API Key", "android-tts-key", InputType.TYPE_CLASS_TEXT);
        etModel = addRow(root, "RVC 模型(.pth)", "Shaonian.pth", InputType.TYPE_CLASS_TEXT);
        etIndex = addRow(root, "索引(.index，可空)", "Shaonian.index", InputType.TYPE_CLASS_TEXT);

        spF0 = addSpinner(root, "f0 算法（rmvpe 最好/最慢）",
                new String[]{"rmvpe", "pm", "dio", "harvest", "fcpe", "crepe"});
        etPitch = addRow(root, "音调 pitch（半音）", "5", InputType.TYPE_CLASS_NUMBER | InputType.TYPE_NUMBER_FLAG_SIGNED);
        etExpire = addRow(root, "音频保留（分钟）", "10", InputType.TYPE_CLASS_NUMBER);
        etConc = addRow(root, "并发数", "1", InputType.TYPE_CLASS_NUMBER);
        etThreads = addRow(root, "CPU 线程数（建议 4~8）", "8", InputType.TYPE_CLASS_NUMBER);
        spFormat = addSpinner(root, "输出格式", new String[]{"wav", "mp3"});

        root.addView(buttonRow(
                btn("保存配置", v -> background(this::saveConfig)),
                btn("启动服务", v -> background(this::startService)),
                btn("停止服务", v -> background(this::stopService)),
                btn("重启服务", v -> background(this::restartService)),
                btn("刷新状态", v -> background(this::refreshStatus))
        ));

        root.addView(divider());
        root.addView(title("测试合成"));
        etText = addRow(root, "文本", "你好，这是安卓手机本地合成的语音测试。", InputType.TYPE_CLASS_TEXT);
        root.addView(buttonRow(
                btn("合成并播放", v -> background(this::testSynthesize)),
                btn("查看日志", v -> background(this::showLogs))
        ));

        tvLog = new TextView(this);
        tvLog.setTextSize(11);
        tvLog.setTextIsSelectable(true);
        tvLog.setBackgroundColor(Color.parseColor("#F2F2F2"));
        tvLog.setPadding(dp(8), dp(8), dp(8), dp(8));
        ScrollView logScroll = new ScrollView(this);
        logScroll.setLayoutParams(new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, dp(220)));
        logScroll.addView(tvLog);
        root.addView(logScroll);

        ScrollView page = new ScrollView(this);
        page.addView(root);
        setContentView(page);

        loadConfig();
    }

    // ===================== 界面小工具 =====================

    private int dp(int v) {
        return (int) (v * getResources().getDisplayMetrics().density);
    }

    private TextView title(String text) {
        TextView tv = new TextView(this);
        tv.setText(text);
        tv.setTextSize(16);
        tv.setPadding(0, dp(8), 0, dp(8));
        return tv;
    }

    private View divider() {
        View v = new View(this);
        v.setLayoutParams(new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(1)));
        v.setBackgroundColor(Color.parseColor("#CCCCCC"));
        return v;
    }

    private EditText addRow(LinearLayout parent, String label, String def, int inputType) {
        LinearLayout row = new LinearLayout(this);
        row.setOrientation(LinearLayout.HORIZONTAL);
        row.setPadding(0, dp(4), 0, dp(4));
        TextView tv = new TextView(this);
        tv.setText(label);
        tv.setLayoutParams(new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f));
        EditText et = new EditText(this);
        et.setInputType(inputType);
        et.setText(def);
        et.setLayoutParams(new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1.2f));
        row.addView(tv);
        row.addView(et);
        parent.addView(row);
        return et;
    }

    private Spinner addSpinner(LinearLayout parent, String label, String[] items) {
        LinearLayout row = new LinearLayout(this);
        row.setOrientation(LinearLayout.HORIZONTAL);
        row.setPadding(0, dp(4), 0, dp(4));
        TextView tv = new TextView(this);
        tv.setText(label);
        tv.setLayoutParams(new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f));
        Spinner sp = new Spinner(this);
        ArrayAdapter<String> adapter = new ArrayAdapter<>(this,
                android.R.layout.simple_spinner_dropdown_item, items);
        sp.setAdapter(adapter);
        sp.setLayoutParams(new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1.2f));
        row.addView(tv);
        row.addView(sp);
        parent.addView(row);
        return sp;
    }

    private Button btn(String text, View.OnClickListener listener) {
        Button b = new Button(this);
        b.setText(text);
        b.setAllCaps(false);
        b.setOnClickListener(listener);
        return b;
    }

    private View buttonRow(Button... buttons) {
        LinearLayout row = new LinearLayout(this);
        row.setOrientation(LinearLayout.HORIZONTAL);
        row.setGravity(Gravity.START);
        for (Button b : buttons) {
            LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                    0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f);
            b.setLayoutParams(lp);
            b.setTextSize(12);
            row.addView(b);
        }
        return row;
    }

    // ===================== 线程 / 提示 =====================

    private interface Task {
        void run() throws Exception;
    }

    private void background(Task task) {
        new Thread(() -> {
            try {
                task.run();
            } catch (Exception e) {
                ui(() -> tvStatus.setText("❌ " + e));
            }
        }).start();
    }

    private void ui(Runnable r) {
        runOnUiThread(r);
    }

    private void toast(String msg) {
        ui(() -> Toast.makeText(this, msg, Toast.LENGTH_SHORT).show());
    }

    /** 以 root 身份执行 shell 命令（服务控制都要 root）。 */
    private synchronized String suBin() {
        if (suBinary != null) return suBinary;
        for (String candidate : SU_CANDIDATES) {
            if (!candidate.equals("su") && !new File(candidate).exists()) continue;
            suBinary = candidate;
            return suBinary;
        }
        suBinary = "su";
        return suBinary;
    }

    private String sh(String cmd) throws Exception {
        Process p = Runtime.getRuntime().exec(new String[]{suBin(), "-c", cmd});
        String out = readAll(p.getInputStream());
        String err = readAll(p.getErrorStream());
        int code = p.waitFor();
        String text = out + (err.isEmpty() ? "" : "\n" + err);
        if (code != 0) {
            text += "\n(退出码 " + code + "：如果提示权限不足，请在 KernelSU/Magisk 里允许本 App 获取 root)";
        }
        return text;
    }

    private static String readAll(InputStream in) throws Exception {
        ByteArrayOutputStream bos = new ByteArrayOutputStream();
        byte[] buf = new byte[4096];
        int n;
        while ((n = in.read(buf)) > 0) bos.write(buf, 0, n);
        return new String(bos.toByteArray(), StandardCharsets.UTF_8);
    }

    private String ctlPath() {
        for (String path : CTL_CANDIDATES) {
            try {
                if (sh("[ -f " + path + " ] && echo yes").contains("yes")) return path;
            } catch (Exception ignored) {
            }
        }
        return CTL_CANDIDATES[0];
    }

    // ===================== 配置读写 =====================

    private void loadConfig() {
        background(() -> {
            String yaml = sh("cat " + CONFIG + " 2>/dev/null");
            if (yaml == null || yaml.trim().isEmpty()) {
                ui(() -> tvStatus.setText("未找到配置（服务端可能还没安装）"));
                return;
            }
            String port = pick(yaml, "port", "8080");
            String key = pick(yaml, "api_key", "");
            String model = pick(yaml, "model", "Shaonian.pth");
            String index = pick(yaml, "index", "");
            String pitch = pick(yaml, "pitch", "5");
            String expire = pick(yaml, "expire_minutes", "10");
            String conc = pick(yaml, "max_concurrent", "1");
            String f0 = pick(yaml, "f0_method", "rmvpe");
            String format = pick(yaml, "output_format", "wav");
            int threads = prefs.getInt("threads", 8);
            ui(() -> {
                etPort.setText(port);
                etKey.setText(key);
                etModel.setText(model);
                etIndex.setText(index);
                etPitch.setText(pitch);
                etExpire.setText(expire);
                etConc.setText(conc);
                etThreads.setText(String.valueOf(threads));
                setSpinner(spF0, f0);
                setSpinner(spFormat, format);
                tvStatus.setText("已读取配置：端口 " + port + "，模型 " + model);
            });
        });
    }

    private static String pick(String yaml, String key, String def) {
        Matcher m = Pattern.compile("^\\s*" + key + ":\\s*\"?([^\"\\n]*)\"?\\s*$", Pattern.MULTILINE)
                .matcher(yaml);
        return m.find() && !m.group(1).trim().isEmpty() ? m.group(1).trim() : def;
    }

    private void setSpinner(Spinner sp, String value) {
        for (int i = 0; i < sp.getCount(); i++) {
            if (value.equals(sp.getItemAtPosition(i))) {
                sp.setSelection(i);
                return;
            }
        }
    }

    private void saveConfig() {
        String port = text(etPort), key = text(etKey), model = text(etModel), index = text(etIndex);
        String f0 = spF0.getSelectedItem().toString();
        String pitch = text(etPitch), expire = text(etExpire), conc = text(etConc);
        String format = spFormat.getSelectedItem().toString();
        int threads = parseInt(text(etThreads), 8);

        String yaml = "# 由安卓 App 生成：/data/tts-rootfs/opt/tts-server/config.yaml\n"
                + "server:\n  host: \"0.0.0.0\"\n  port: " + port + "\n  log_level: \"INFO\"\n"
                + "security:\n  api_key: \"" + key + "\"\n"
                + "tts:\n  source: \"edgetts\"\n  speaker: \"zh-CN-YunxiNeural\"\n  pitch: " + pitch + "\n"
                + "rvc:\n  enabled: true\n"
                + "  model: \"" + model + "\"\n  model_dir: \"./models\"\n  index: \"" + index + "\"\n"
                + "  f0_method: \"" + f0 + "\"\n  device: \"cpu\"\n  is_half: false\n  preload: true\n"
                + "storage:\n  output_dir: \"./output\"\n  expire_minutes: " + expire + "\n"
                + "  cleanup_interval_minutes: 2\n  cache_by_text: true\n  max_text_length: 1000\n"
                + "queue:\n  max_concurrent: " + conc + "\n  max_queue_size: 8\n  timeout: 300\n"
                + "audio:\n  output_format: \"" + format + "\"\n";

        try {
            String b64 = Base64.getEncoder().encodeToString(yaml.getBytes(StandardCharsets.UTF_8));
            sh("echo '" + b64 + "' | base64 -d > " + CONFIG + " && echo OK");
            prefs.edit().putInt("threads", threads).apply();
            ui(() -> tvStatus.setText("✔ 配置已保存（线程数 " + threads + "，重启服务生效）"));
            toast("配置已保存");
        } catch (Exception e) {
            ui(() -> tvStatus.setText("❌ 保存失败：" + e));
        }
    }

    private static String text(EditText et) {
        return et.getText().toString().trim();
    }

    private static int parseInt(String s, int def) {
        try {
            return Integer.parseInt(s.trim());
        } catch (Exception e) {
            return def;
        }
    }

    // ===================== 服务控制 =====================

    private void startService() {
        try {
            int threads = parseInt(text(etThreads), 8);
            sh("OMP_NUM_THREADS=" + threads + " sh " + ctlPath() + " start");
            String st = sh("sh " + ctlPath() + " status");
            ui(() -> tvStatus.setText(st));
        } catch (Exception e) {
            ui(() -> tvStatus.setText("❌ 启动失败：" + e));
        }
    }

    private void stopService() {
        try {
            String out = sh("sh " + ctlPath() + " stop");
            ui(() -> tvStatus.setText(out));
        } catch (Exception e) {
            ui(() -> tvStatus.setText("❌ 停止失败：" + e));
        }
    }

    private void restartService() {
        try {
            int threads = parseInt(text(etThreads), 8);
            sh("sh " + ctlPath() + " stop");
            sh("OMP_NUM_THREADS=" + threads + " sh " + ctlPath() + " start");
            String status = sh("sh " + ctlPath() + " status");
            ui(() -> tvStatus.setText(status));
        } catch (Exception e) {
            ui(() -> tvStatus.setText("❌ 重启失败：" + e));
        }
    }

    private void refreshStatus() {
        try {
            JSONObject health = httpJson("GET", "/api/health", null, null);
            JSONObject engine = health.optJSONObject("engine");
            JSONObject storage = health.optJSONObject("storage");
            StringBuilder sb = new StringBuilder();
            sb.append("状态: ").append(health.optString("status"))
                    .append(" | 引擎就绪: ").append(engine != null && engine.optBoolean("ready"))
                    .append('\n');
            if (engine != null) {
                sb.append("设备: ").append(engine.optString("device"))
                        .append(" | 模型: ").append(fileName(engine.optString("model")))
                        .append('\n');
                sb.append("发音人: ").append(engine.optString("speaker"))
                        .append(" | 音调: ").append(engine.optInt("pitch"))
                        .append(" | f0: ").append(engine.optString("f0_method")).append('\n');
                JSONObject st = engine.optJSONObject("stats");
                if (st != null) {
                    sb.append("累计推理 ").append(st.optInt("total"))
                            .append(" 次 | 成功 ").append(st.optInt("success"))
                            .append(" | 失败 ").append(st.optInt("failed"))
                            .append(" | 缓存命中 ").append(st.optInt("cache_hit")).append('\n');
                    if (st.optDouble("last_duration", 0) > 0) {
                        sb.append("最近一次耗时: ").append(st.optDouble("last_duration")).append("s\n");
                    }
                }
            }
            if (storage != null) {
                sb.append("音频保留: ").append(storage.optInt("expire_minutes")).append(" 分钟 | 现有 ")
                        .append(storage.optInt("files")).append(" 个文件\n");
            }
            ui(() -> tvStatus.setText(sb.toString()));
        } catch (Exception e) {
            ui(() -> tvStatus.setText("❌ 服务未响应：" + e.getMessage()
                    + "\n（请先点「启动服务」，或检查端口/API Key）"));
        }
    }

    private void showLogs() {
        try {
            String out = sh("tail -n 60 " + LOG + " 2>/dev/null");
            ui(() -> tvLog.setText(out == null || out.trim().isEmpty() ? "(暂无日志)" : out));
        } catch (Exception e) {
            ui(() -> tvLog.setText("读取日志失败：" + e));
        }
    }

    /** 直接调用本地服务合成一句话并播放 */
    private void testSynthesize() {
        String text = text(etText);
        if (text.isEmpty()) {
            toast("请输入要合成的文本");
            return;
        }
        try {
            String key = text(etKey);
            long t0 = System.currentTimeMillis();
            byte[] audio = httpBytes("/api/tts/file", text, key);
            long cost = System.currentTimeMillis() - t0;
            File out = new File(getCacheDir(), "test.wav");
            try (FileOutputStream fos = new FileOutputStream(out)) {
                fos.write(audio);
            }
            ui(() -> {
                tvStatus.setText("✔ 合成成功：" + (audio.length / 1024) + " KB，耗时 "
                        + String.format("%.1f", cost / 1000.0) + "s\n文件：" + out.getAbsolutePath());
            });
            MediaPlayer mp = new MediaPlayer();
            mp.setDataSource(out.getAbsolutePath());
            mp.prepare();
            mp.start();
        } catch (Exception e) {
            ui(() -> tvStatus.setText("❌ 合成失败：" + e.getMessage()));
        }
    }

    // ===================== HTTP =====================

    private String baseUrl() {
        return "http://127.0.0.1:" + text(etPort);
    }

    private byte[] httpBytes(String path, String text, String apiKey) throws Exception {
        HttpURLConnection conn = (HttpURLConnection) new URL(baseUrl() + path).openConnection();
        conn.setRequestMethod("POST");
        conn.setConnectTimeout(8000);
        conn.setReadTimeout(600000);
        conn.setDoOutput(true);
        conn.setRequestProperty("Content-Type", "application/json");
        if (apiKey != null && !apiKey.isEmpty()) {
            conn.setRequestProperty("Authorization", "Bearer " + apiKey);
        }
        JSONObject body = new JSONObject();
        body.put("text", text);
        try (OutputStream os = conn.getOutputStream()) {
            os.write(body.toString().getBytes(StandardCharsets.UTF_8));
        }
        int code = conn.getResponseCode();
        InputStream in = code >= 400 ? conn.getErrorStream() : conn.getInputStream();
        ByteArrayOutputStream bos = new ByteArrayOutputStream();
        byte[] buf = new byte[8192];
        int n;
        while (in != null && (n = in.read(buf)) > 0) bos.write(buf, 0, n);
        if (code != 200) {
            throw new Exception("HTTP " + code + ": " + bos.toString("UTF-8"));
        }
        return bos.toByteArray();
    }

    private JSONObject httpJson(String method, String path, String body, String apiKey) throws Exception {
        HttpURLConnection conn = (HttpURLConnection) new URL(baseUrl() + path).openConnection();
        conn.setRequestMethod(method);
        conn.setConnectTimeout(5000);
        conn.setReadTimeout(20000);
        if (body != null) {
            conn.setDoOutput(true);
            conn.setRequestProperty("Content-Type", "application/json");
            try (OutputStream os = conn.getOutputStream()) {
                os.write(body.getBytes(StandardCharsets.UTF_8));
            }
        }
        if (apiKey != null && !apiKey.isEmpty()) {
            conn.setRequestProperty("Authorization", "Bearer " + apiKey);
        }
        int code = conn.getResponseCode();
        InputStream in = code >= 400 ? conn.getErrorStream() : conn.getInputStream();
        BufferedReader reader = new BufferedReader(new InputStreamReader(in, StandardCharsets.UTF_8));
        StringBuilder sb = new StringBuilder();
        String line;
        while ((line = reader.readLine()) != null) sb.append(line);
        if (code != 200) throw new Exception("HTTP " + code + " " + sb);
        return new JSONObject(sb.toString());
    }

    private static String fileName(String path) {
        if (path == null || path.isEmpty()) return "-";
        int i = path.lastIndexOf('/');
        return i >= 0 ? path.substring(i + 1) : path;
    }
}
