import subprocess
import time
import datetime
import os
import re
import argparse
import csv
from pathlib import Path

DESKTOP_DIR = os.path.expanduser("~/Desktop")
DEFAULT_HOST = "http://127.0.0.1:8000"
OUT_DIR = "auto_results"

METRICS_CMD_TEMPLATE = (
    "{curl} -s {host}/metrics | "
    "grep 'vllm:num_requests_running' | "
    "tail -n 1"
)

METRICS_WAITING_CMD_TEMPLATE = (
    "{curl} -s {host}/metrics | "
    "grep 'vllm:num_requests_waiting' | "
    "tail -n 1"
)

def now_string():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def run_id_string():
    return datetime.datetime.now().strftime("%Y%m%d_%H%M%S")


def expand_path(p):
    return str(Path(p).expanduser())


def parse_args():
    parser = argparse.ArgumentParser()

    parser.add_argument("--model-key", required=True, help="选择要跑的模型配置 key")
    parser.add_argument("--host", default=DEFAULT_HOST)

    # 你要的：可手动改 docker exec 的写法（保留默认）
    # 默认等价于你人工用的：sudo docker exec ...
    parser.add_argument(
        "--docker-exec-prefix",
        default="sudo docker exec",
        help='Docker exec 前缀命令（默认: "sudo docker exec"）'
    )

    # 你要的：可手动改 locust-env 的路径（保留默认）
    # 默认等价于你人工 source 的环境：~/locust-env
    parser.add_argument(
        "--venv-path",
        default="~/locust-env",
        help="locust-env 虚拟环境路径（默认: ~/locust-env）"
    )

    # 容器名
    parser.add_argument("--container-name", required=True, help="运行 vLLM 的容器名（docker ps 里看到的名字）")

    # ready 检查
    parser.add_argument("--ready-timeout-seconds", type=int, default=180)
    parser.add_argument("--skip-start-server", action="store_true")
    parser.add_argument("--skip-stop-server", action="store_true")

    # QPS 扫描参数
    parser.add_argument("--qps-start", type=int, default=10)
    parser.add_argument("--qps-step", type=int, default=5)
    parser.add_argument("--qps-max", type=int, default=200)

    # 平台期参数
    parser.add_argument("--plateau-seconds", type=int, default=15)
    parser.add_argument("--poll-seconds", type=int, default=1)

    # locust 运行时间与 token 参数
    parser.add_argument("--main-runtime-seconds", type=int, default=99999)
    parser.add_argument("--probe-runtime-seconds", type=int, default=120)

    parser.add_argument("--main-max-tokens", type=int, default=1000)
    parser.add_argument("--probe-max-tokens", type=int, default=100)

    # users / spawn_rate 与 qps 的倍率
    parser.add_argument("--users-multiplier", type=float, default=1.5)
    parser.add_argument("--spawn-multiplier", type=float, default=1.5)

    # 可选：curl 可指定
    parser.add_argument("--curl-bin", default="curl")

    return parser.parse_args()


def split_prefix(prefix_str):
    # 把 "sudo docker exec" 这种字符串拆成 ["sudo","docker","exec"]
    # 不做复杂 shell 解析（你这里就是简单前缀）
    parts = []
    for x in prefix_str.strip().split():
        if x != "":
            parts.append(x)
    return parts


def docker_exec(args, container_name, bash_cmd):
    """
    等价于：sudo docker exec <container> bash -lc "<bash_cmd>"
    但 sudo/docker/exec 都可由 --docker-exec-prefix 改
    """
    prefix = split_prefix(args.docker_exec_prefix)
    cmd = prefix + [container_name, "bash", "-lc", bash_cmd]
    print("[" + now_string() + "] DOCKER:", " ".join(cmd))
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def start_vllm_in_container(args, container_name, serve_cmd, pid_file_path, log_file_path):
    wrapped = (
        "rm -f {pid}; "
        "nohup {serve} > {log} 2>&1 & "
        "echo $! > {pid}; "
        "sleep 1; "
        "cat {pid}"
    ).format(pid=pid_file_path, serve=serve_cmd, log=log_file_path)

    print("[" + now_string() + "] 在容器内启动 vLLM（后台）...")
    result = docker_exec(args, container_name, wrapped)
    print(result.stdout.strip())

    pid_text = result.stdout.strip()
    if pid_text == "":
        raise RuntimeError("容器内启动 vLLM 失败：没有输出 PID。")

    last_line = pid_text.splitlines()[-1].strip()
    if not last_line.isdigit():
        raise RuntimeError("容器内启动 vLLM 失败：拿到的 PID 不是数字。请检查容器日志。")

    print("[" + now_string() + "] vLLM PID =", last_line)
    return last_line


def stop_vllm_in_container(args, container_name, pid_file_path):
    print("[" + now_string() + "] 尝试停止容器内 vLLM...")

    check = docker_exec(args, container_name, "test -f {p} && echo OK || echo NO".format(p=pid_file_path))
    if "OK" not in check.stdout:
        print("[" + now_string() + "] 找不到 PID 文件，跳过停止。")
        return

    docker_exec(args, container_name, "kill -TERM $(cat {p}) 2>/dev/null || true".format(p=pid_file_path))
    time.sleep(2)
    docker_exec(args, container_name, "kill -KILL $(cat {p}) 2>/dev/null || true".format(p=pid_file_path))
    docker_exec(args, container_name, "rm -f {p}".format(p=pid_file_path))

    print("[" + now_string() + "] 已发送停止信号给 vLLM（TERM/KILL）")


def wait_for_ready(args, host, timeout_seconds):
    print("[" + now_string() + "] 等待服务就绪:", host)
    start = time.time()
    url = host.rstrip("/") + "/v1/models"

    while True:
        try:
            result = subprocess.run(
                [args.curl_bin, "-s", url],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=3
            )
            if result.returncode == 0 and result.stdout.strip() != "":
                print("[" + now_string() + "] 服务已就绪（/v1/models 有返回）")
                return
        except Exception:
            pass

        if time.time() - start >= timeout_seconds:
            raise RuntimeError("等待服务就绪超时（{t}s）。".format(t=timeout_seconds))

        time.sleep(2)


def get_num_requests_running(args, host):
    metrics_cmd = METRICS_CMD_TEMPLATE.format(curl=args.curl_bin, host=host.rstrip("/"))
    line = subprocess.getoutput(metrics_cmd).strip()
    if line == "":
        return None

    parts = line.split()
    if len(parts) < 2:
        return None

    try:
        return float(parts[-1])
    except ValueError:
        return None

def get_num_requests_waiting(args, host):
    metrics_cmd = METRICS_WAITING_CMD_TEMPLATE.format(
        curl=args.curl_bin,
        host=host.rstrip("/")
    )
    line = subprocess.getoutput(metrics_cmd).strip()
    if line == "":
        return None

    parts = line.split()
    if len(parts) < 2:
        return None

    try:
        return float(parts[-1])
    except ValueError:
        return None

def build_main_locust_cmd(args, host, model_path, tokenizer_path,
                          users, spawn_rate, qps,
                          run_time_s, max_tokens,
                          extra_flags):
    cmd = [
        args.locust_bin,                 # <- 用 venv 的 locust
        "-f", "load_test.py",
        "--headless",
        "-H", host,
        "--provider", "vllm",
        "--model", model_path,
        "--tokenizer", tokenizer_path,
        "-u", str(users),
        "-r", str(spawn_rate),
        "--qps", str(qps),
        "-t", str(run_time_s) + "s",
        "--max-tokens", str(max_tokens),
    ]

    if extra_flags is not None:
        for item in extra_flags:
            cmd.append(item)

    return cmd


def build_probe_locust_cmd(args, host, model_path, tokenizer_path,
                           run_time_s, max_tokens,
                           extra_flags):
    cmd = [
        args.locust_bin,                 # <- 用 venv 的 locust
        "-f", "load_test.py",
        "--headless",
        "-H", host,
        "--provider", "vllm",
        "--model", model_path,
        "--tokenizer", tokenizer_path,
        "-u", "1",
        "-r", "1",
        "--qps", "1",
        "-t", str(run_time_s) + "s",
        "--max-tokens", str(max_tokens),
    ]

    if extra_flags is not None:
        for item in extra_flags:
            cmd.append(item)

    return cmd


def start_main_locust(cmd, main_log_path):
    log_file = open(main_log_path, "a", encoding="utf-8")
    log_file.write("[" + now_string() + "] MAIN CMD: " + " ".join(cmd) + "\n")

    proc = subprocess.Popen(
        cmd,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        text=True
    )
    return proc, log_file


def run_probe_and_get_ttft(cmd, probe_log_path):
    print("[" + now_string() + "] 开始探针（" + cmd[cmd.index("-t") + 1] + "）...")

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

    ttft_value = None
    for line in result.stdout.splitlines():
        line_stripped = line.strip()
        line_lower = line_stripped.lower()

    # 只抓这一行：Time To First Token : <number>
    # 排除：P50 Time To First Token : <number>
    # 排除：METRIC time_to_first_token ...（没有冒号，且不以 time to first token 开头）
        if line_lower.startswith("time to first token") and ":" in line_lower:
        # 排除分位数行（P50/P90/...）
            if line_lower.startswith("p"):

                continue


            nums = re.findall(r"[0-9]+(?:\.[0-9]+)?", line_stripped)
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


def wait_until_plateau(args, host, watcher_log_path, poll_seconds, plateau_seconds):
    print("[" + now_string() + "] 等待 num_requests_running 先变成 > 0 (证明主压测真的打到了模型)...")

    while True:
        cur = get_num_requests_running(args, host)

        with open(watcher_log_path, "a", encoding="utf-8") as f:
            f.write("[" + now_string() + "] running=" + str(cur) + "\n")

        print("[" + now_string() + "] 当前 num_requests_running =", cur)

        if cur is not None and cur > 0:
            break

        time.sleep(poll_seconds)

    last_value = cur
    last_increase_time = time.time()

    print("[" + now_string() + "] 已确认主压测在打请求，开始平台期检测（{s}秒不增长）...".format(s=plateau_seconds))

    while True:
        cur = get_num_requests_running(args, host)
        now = time.time()

        with open(watcher_log_path, "a", encoding="utf-8") as f:
            f.write("[" + now_string() + "] running=" + str(cur) + "\n")

        print("[" + now_string() + "] 当前 num_requests_running =", cur)

        if cur is not None and cur > last_value:
            last_value = cur
            last_increase_time = now

        if now - last_increase_time >= plateau_seconds:
            print("[" + now_string() + "] 连续 {s} 秒不增长，平台期到了。".format(s=plateau_seconds))

            final_running = last_value
            final_waiting = get_num_requests_waiting(args, host)

            with open(watcher_log_path, "a", encoding="utf-8") as f:
                f.write("[" + now_string() + "] waiting=" + str(final_waiting) + "\n")

            return final_running, final_waiting

        time.sleep(poll_seconds)


def main():
    args = parse_args()

    # 用 venv-path 计算出 locust/python 的绝对路径（不需要 source activate）
    venv_path = expand_path(args.venv_path)
    locust_bin = os.path.join(venv_path, "bin", "locust")
    python_bin = os.path.join(venv_path, "bin", "python")

    args.locust_bin = locust_bin
    args.python_bin = python_bin

    if not os.path.exists(args.locust_bin):
        raise RuntimeError("找不到 locust 可执行文件: " + args.locust_bin + "（请检查 --venv-path）")

    # 模型配置表：你按真实路径改
    MODELS = {
        "qwen3-8b": {
            "model_path": "/data/models/Qwen3-8B",
            "tokenizer_path": "/data/models/Qwen3-8B",
            "serve_cmd": "vllm serve /data/models/Qwen3-8B --host 0.0.0.0 --port 8000",
            "main_extra_flags": [],
            "probe_extra_flags": [],
        }
    }

    if args.model_key not in MODELS:
        print("[" + now_string() + "] 找不到 model-key:", args.model_key)
        print("可用 model-key：")
        for k in MODELS.keys():
            print(" -", k)
        return

    model_conf = MODELS[args.model_key]
    model_path = model_conf["model_path"]
    tokenizer_path = model_conf["tokenizer_path"]
    serve_cmd = model_conf["serve_cmd"]
    main_extra_flags = model_conf["main_extra_flags"]
    probe_extra_flags = model_conf["probe_extra_flags"]

    pid_file_path = "/tmp/vllm_serve.pid"
    log_file_path = "/tmp/vllm_serve.log"

    rid = run_id_string()
    out_path = os.path.join(OUT_DIR, args.model_key + "_" + rid)
    os.makedirs(out_path, exist_ok=True)

    watcher_log = os.path.join(out_path, "watcher.log")
    results_csv = os.path.join(out_path, "results.csv")

    with open(results_csv, "w", encoding="utf-8") as f:
        #f.write("time,model_key,main_qps,plateau_running,probe_ttft,test_time,main_log,probe_log\n")
        f.write("qps,user,spawn,run,wait,probe_ttft,test_time,probe_log\n")

    # 1) 启动 vLLM（容器内）
    if not args.skip_start_server:
        start_vllm_in_container(
            args=args,
            container_name=args.container_name,
            serve_cmd=serve_cmd,
            pid_file_path=pid_file_path,
            log_file_path=log_file_path
        )
    else:
        print("[" + now_string() + "] 已设置 --skip-start-server，跳过启动 vLLM")

    # 2) 等 ready
    wait_for_ready(args, args.host, args.ready_timeout_seconds)

    # 3) QPS 扫描
    qps = args.qps_start

    try:
        while qps <= args.qps_max:
            print("\n==============================")
            print("[" + now_string() + "] 新一轮开始：model =", args.model_key, "main QPS =", qps)
            print("==============================\n")

            users = int(qps * args.users_multiplier)
            spawn_rate = int(qps * args.spawn_multiplier)

            main_log = os.path.join(out_path, "main_qps_" + str(qps) + ".log")
            probe_log = os.path.join(out_path, "probe_at_qps_" + str(qps) + ".log")

            main_cmd = build_main_locust_cmd(
                args=args,
                host=args.host,
                model_path=model_path,
                tokenizer_path=tokenizer_path,
                users=users,
                spawn_rate=spawn_rate,
                qps=qps,
                run_time_s=args.main_runtime_seconds,
                max_tokens=args.main_max_tokens,
                extra_flags=main_extra_flags
            )

            probe_cmd = build_probe_locust_cmd(
                args=args,
                host=args.host,
                model_path=model_path,
                tokenizer_path=tokenizer_path,
                run_time_s=args.probe_runtime_seconds,
                max_tokens=args.probe_max_tokens,
                extra_flags=probe_extra_flags
            )

            main_proc, main_log_file = start_main_locust(main_cmd, main_log)

            plateau_value, plateau_waiting = wait_until_plateau(
                args=args,
                host=args.host,
                watcher_log_path=watcher_log,
                poll_seconds=args.poll_seconds,
                plateau_seconds=args.plateau_seconds
            )

            ttft = run_probe_and_get_ttft(probe_cmd, probe_log)
            print("[" + now_string() + "] 探针结束，TTFT =", ttft)

            stop_main(main_proc, main_log_file)

            with open(results_csv, "a", encoding="utf-8") as f:
                f.write(str(qps) + ",")
                f.write(str(users) + ",")               
                f.write(str(spawn_rate) + ",")
                #f.write(now_string() + ",")
                #f.write(args.model_key + ",")
                #f.write(str(qps) + ",")
                f.write(str(plateau_value) + ",")
                f.write(str(plateau_waiting) + ",")
                f.write(str(ttft) + ",")
                f.write(str(args.probe_runtime_seconds) + ",")
                #f.write(main_log + ",")
                f.write(probe_log + "\n")

            qps = qps + args.qps_step
            time.sleep(2)

    finally:
        # 4) 停 vLLM（容器内）
        if not args.skip_stop_server:
            stop_vllm_in_container(args, args.container_name, pid_file_path)
        else:
            print("[" + now_string() + "] 已设置 --skip-stop-server，跳过停止 vLLM")

        print("[" + now_string() + "] 全部完成。结果在：", out_path)
        print("CSV：", results_csv)
        print("容器内 vLLM 日志：", log_file_path, "(在容器内查看)")



    print("\n========== FINAL SUMMARY ==========\n")

    with open(results_csv, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        rows = list(reader)

    # 打印表头
    header = rows[0]
    print("{:<20} {:<}".format(header[0], header[1]))

    # 打印分隔线
    print("-" * 60)

    # 打印每一行
    for row in rows[1:]:
        print("{:<20} {:<}".format(row[0], row[1]))

if __name__ == "__main__":

    main()
