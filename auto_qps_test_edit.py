import subprocess
import time
import datetime
import os
import re


HOST = "http://127.0.0.1:8000"

# 得到正在进行的请求数量
METRICS_CMD = (
    "curl -s 127.0.0.1:8000/metrics | "
    "grep 'vllm:num_requests_running' | "
    "tail -n 1"
)


# 可调参数
QPS_START = 10
QPS_STEP = 5
QPS_MAX = 200

PLATEAU_SECONDS = 60
POLL_SECONDS = 1

# 如果你原来“跑得通”的 locust 命令里需要额外参数（最常见就是 --chat）
# 就写在这里，比如：MAIN_EXTRA_FLAGS = ["--chat"]
# 不需要就留空列表：[]
MAIN_EXTRA_FLAGS = []   # 如果你平时必须用 --chat，就改成 ["--chat"]

PROBE_EXTRA_FLAGS = []  # 同理：探针需要什么额外参数也写这里

OUT_DIR = "auto_results"


def now_string():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def run_id_string():
    return datetime.datetime.now().strftime("%Y%m%d_%H%M%S")


def get_num_requests_running():
    line = subprocess.getoutput(METRICS_CMD).strip()
    if line == "":
        return None

    parts = line.split()
    if len(parts) < 2:
        return None

    try:
        return float(parts[-1])
    except ValueError:
        return None


def start_main_locust(qps, main_log_path):
    cmd = [
        "locust",
        "-f", "load_test.py",
        "--headless",
        "-H", HOST,
        "--provider", "vllm",
        "--model", "/data/models/Qwen3-8B",
        "--tokenizer", "/data/models/Qwen3-8B",
        "-u", "50",
        "-r", "50",
        "--qps", str(qps),
        "-t", "99999s",
        "--max-tokens", "1000",
    ] + MAIN_EXTRA_FLAGS

    log_file = open(main_log_path, "a", encoding="utf-8")
    log_file.write("[" + now_string() + "] MAIN CMD: " + " ".join(cmd) + "\n")

    proc = subprocess.Popen(
        cmd,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        text=True
    )
    return proc, log_file


def run_probe_and_get_ttft(probe_log_path):
    cmd = [
        "locust",
        "-f", "load_test.py",
        "--headless",
        "-H", HOST,
        "--provider", "vllm",
        "--model", "/data/models/Qwen3-8B",
        "--tokenizer", "/data/models/Qwen3-8B",
        "-u", "1",
        "-r", "1",
        "--qps", "1",
        "-t", "120s",
        "--max-tokens", "100",
    ] + PROBE_EXTRA_FLAGS

    print("[" + now_string() + "] 开始探针（120s）...")

    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True
    )

    with open(probe_log_path, "a", encoding="utf-8") as f:
        f.write("[" + now_string() + "] PROBE CMD: " + " ".join(cmd) + "\n")
        f.write(result.stdout)
        f.write("\n")

    # 尝试抓 TTFT：先找包含 ttft 的行，再抓里面的数字
    # 用来直接抓取ttft的代码（可以删除）
    ttft_value = None
    for line in result.stdout.splitlines():
        if "ttft" in line.lower():
            nums = re.findall(r"[0-9]+(?:\.[0-9]+)?", line)
            if len(nums) > 0:
                ttft_value = nums[-1]

    return ttft_value


def stop_main(proc, log_file):
    print("[" + now_string() + "] 停止主压测...")
    if proc.poll() is None:
        proc.terminate()
        time.sleep(2)
    if proc.poll() is None:
        proc.kill()

    try:
        log_file.close()
    except Exception:
        pass


def wait_until_plateau(watcher_log_path):
    """
    关键改动：
    1) 先等到 num_requests_running > 0 至少出现一次（证明 locust 真打到了模型）
    2) 然后再开始“60秒不增长=平台期”的判断
    """
    print("[" + now_string() + "] 等待 num_requests_running 先变成 > 0 (证明主压测真的打到了模型)...")

    # 先等它 > 0
    while True:
        cur = get_num_requests_running()
        with open(watcher_log_path, "a", encoding="utf-8") as f:
            f.write("[" + now_string() + "] running=" + str(cur) + "\n")

        print("[" + now_string() + "] 当前 num_requests_running =", cur)

        if cur is not None and cur > 0:
            break

        time.sleep(POLL_SECONDS)

    # 出现 >0 后，再做平台期计时
    last_value = cur
    last_increase_time = time.time()

    print("[" + now_string() + "] 已确认主压测在打请求，开始平台期检测（30秒不增长）...")

    while True:
        cur = get_num_requests_running()
        now = time.time()

        with open(watcher_log_path, "a", encoding="utf-8") as f:
            f.write("[" + now_string() + "] running=" + str(cur) + "\n")

        print("[" + now_string() + "] 当前 num_requests_running =", cur)

        if cur is not None and cur > last_value:
            last_value = cur
            last_increase_time = now

        if now - last_increase_time >= PLATEAU_SECONDS:
            print("[" + now_string() + "] 连续 30 秒不增长，平台期到了。")
            return last_value

        time.sleep(POLL_SECONDS)


def main():
    rid = run_id_string()
    out_path = os.path.join(OUT_DIR, rid)
    os.makedirs(out_path, exist_ok=True)

    watcher_log = os.path.join(out_path, "watcher.log")
    results_csv = os.path.join(out_path, "results.csv")

    with open(results_csv, "w", encoding="utf-8") as f:
        f.write("time,main_qps,plateau_running,probe_ttft,main_log,probe_log\n")

    qps = QPS_START

    while qps <= QPS_MAX:
        print("\n==============================")
        print("[" + now_string() + "] 新一轮开始：main QPS =", qps)
        print("==============================\n")

        main_log = os.path.join(out_path, "main_qps_" + str(qps) + ".log")
        probe_log = os.path.join(out_path, "probe_at_qps_" + str(qps) + ".log")

        main_proc, main_log_file = start_main_locust(qps, main_log)

        plateau_value = wait_until_plateau(watcher_log)

        ttft = run_probe_and_get_ttft(probe_log)
        print("[" + now_string() + "] 探针结束，TTFT =", ttft)

        # 你要求：探针出结果后，立刻停主压测
        stop_main(main_proc, main_log_file)

        with open(results_csv, "a", encoding="utf-8") as f:
            f.write(now_string() + ",")
            f.write(str(qps) + ",")
            f.write(str(plateau_value) + ",")
            f.write(str(ttft) + ",")
            f.write(main_log + ",")
            f.write(probe_log + "\n")

        qps = qps + QPS_STEP
        time.sleep(2)

    print("[" + now_string() + "] 全部完成。结果在：", out_path)
    print("CSV：", results_csv)


if __name__ == "__main__":
    main()


