import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.dates import DayLocator, HourLocator
from matplotlib.ticker import FuncFormatter
import os
import numpy as np
import duckdb  # <--- 新增依赖

# ===================== 1. 路径配置 =====================
current_dir = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(current_dir, "static")

# ========= 多数据源统一配置 =========
PLOT_CONFIGS = [
    {
        "data_file": "/root/data/soil1.csv",
        "plot_path": os.path.join(STATIC_DIR, "plot1.png"),
        "title_suffix": "Soil-1"
    },
    {
         "data_file": "/root/data/soil2.csv",
         "plot_path": os.path.join(STATIC_DIR, "plot2.png"),
         "title_suffix": "Soil-2"
     },
     {
         "data_file": "/root/data/soil3.csv",
         "plot_path": os.path.join(STATIC_DIR, "plot3.png"),
         "title_suffix": "Soil-3"
     },
     {
         "data_file": "/root/data/water-test.csv",
         "plot_path": os.path.join(STATIC_DIR, "test.png"),
         "title_suffix": "Soil-test"
     }
]
# =====================================================


# ===================== 2. 读取 CSV —— DuckDB 降维打击版 =====================
def load_and_process_data(data_file):
    """使用 DuckDB 边读边过滤，解决超大 CSV 内存溢出问题"""

    if not os.path.exists(data_file):
        print(f"[ERROR] 数据文件不存在: {data_file}")
        return pd.DataFrame()

    print(f"正在处理: {data_file} ...")
    df = None

    # ==== 核心提速方案：DuckDB 流式读取 ====
    try:
        con = duckdb.connect()
        # 1. 扫描文件，找出最新时间（不加载全量数据进内存）
        # all_varchar=True 防止脏数据导致类型推断报错
        q_max = f"""
            SELECT MAX(TRY_CAST(REPLACE("接收时间", '/', '-') AS TIMESTAMP)) as max_t
            FROM read_csv_auto('{data_file}', all_varchar=True, ignore_errors=True)
        """
        max_time_df = con.execute(q_max).df()
        max_t = max_time_df['max_t'][0]

        if pd.isna(max_t):
            raise ValueError("无法解析有效时间，退回 Pandas 模式")

        # 2. 精准切割：只读取最近 15 天的数据
        q_data = f"""
            SELECT "接收时间", "温度(°C)", "湿度(%)", "电导率", "是否浇水", "光照(lux)"
            FROM read_csv_auto('{data_file}', all_varchar=True, ignore_errors=True)
            WHERE TRY_CAST(REPLACE("接收时间", '/', '-') AS TIMESTAMP) >= TIMESTAMP '{max_t}' - INTERVAL 15 DAY
        """
        df = con.execute(q_data).df()
        print("✔ 成功使用 DuckDB 提取最近15天数据，内存极度安全")
        con.close()

    except Exception as e:
        print(f"[WARN] DuckDB 读取失败 ({e})，启动 Pandas 兜底机制...")
        # ==== 兜底方案：沿用你原先的多编码重试逻辑 ====
        encodings_to_try = ['utf-8-sig', 'utf-8', 'gbk', 'gb2312']
        for enc in encodings_to_try:
            try:
                df = pd.read_csv(
                    data_file,
                    na_values=['None', ' ', '', 'null'],
                    encoding=enc,
                    on_bad_lines='skip' # 忽略坏行，防止单行乱码崩盘
                )
                print(f"✔ Pandas 兜底成功，使用编码: {enc}")
                break
            except:
                pass

    if df is None or df.empty:
        print(f"[ERROR] 所有读取尝试失败或数据为空 → {data_file}")
        return pd.DataFrame()

    # ===================== 校验列 =====================
    required_cols = ['接收时间', '温度(°C)', '湿度(%)', '电导率', '光照(lux)']
    for col in required_cols:
        if col not in df.columns:
            print(f"[ERROR] 缺少必要列：{col}")
            return pd.DataFrame()

    # ===================== 时间格式兼容 =====================
    # 使用 mixed 模式，自动通杀带斜杠或带横杠的时间格式
    df['timestamp'] = pd.to_datetime(df['接收时间'], format='mixed', errors='coerce')

    # 删除无效时间
    df = df.dropna(subset=['timestamp'])
    if df.empty:
        print("[ERROR] 所有时间均无效")
        return pd.DataFrame()

    # 排序
    df = df.sort_values('timestamp')

    # 【注意】这里已经去掉了 df.to_csv 的回写逻辑，保护源文件不被意外清空！

    # ===================== 重命名列 =====================
    df.rename(columns={
        '温度(°C)': 'temperature',
        '湿度(%)': 'humidity',
        '电导率': 'conductivity',
        '光照(lux)': 'lighting'
    }, inplace=True)

    # 数值类型清洗
    numeric_cols = ['temperature', 'humidity', 'conductivity', 'lighting']
    for col in numeric_cols:
        df[col] = pd.to_numeric(df[col], errors='coerce')

    df = df.dropna(subset=numeric_cols, how='all')
    if df.empty:
        print("[WARN] 清洗后数据为空")
        return pd.DataFrame()

    # ===================== 5 分钟聚合逻辑 =====================
    df['timestamp'] = df['timestamp'].dt.floor('5min')
    df = df.drop_duplicates('timestamp', keep='last')
    df.set_index('timestamp', inplace=True)

    full_index = pd.date_range(df.index.min(), df.index.max(), freq='5min')
    df = df.reindex(full_index)

    return df[numeric_cols]


# ===================== 3. 绘图函数 (保持你的原汁原味) =====================
def plot_data(df, plot_path, title_suffix=""):
    if df.empty or len(df) < 2:
        print("[WARN] 数据不足，跳过绘图")
        return False

    time_data = df.index.to_pydatetime()

    fig, ax1 = plt.subplots(figsize=(20, 8))

    # 温度
    ax1.plot(time_data, df['temperature'], 'r-', label='Temperature (°C)', linewidth=1, alpha=0.8)
    ax1.set_ylabel('Temperature (°C)', color='r')
    ax1.set_ylim(10, 38)
    ax1.tick_params(axis='y', labelcolor='r')

    # 湿度
    ax2 = ax1.twinx()
    ax2.plot(time_data, df['humidity'], 'b-', label='Humidity (%)', linewidth=1, alpha=0.8)
    ax2.set_ylabel('Humidity (%)', color='b')
    ax2.set_ylim(0, 100)
    ax2.tick_params(axis='y', labelcolor='b')

    # 电导率
    ax3 = ax1.twinx()
    ax3.spines.right.set_position(("axes", 1.1))
    ax3.plot(time_data, df['conductivity'], 'g-', label='Conductivity', linewidth=1, alpha=0.8)
    ax3.set_ylabel('Conductivity', color='g')
    ax3.set_ylim(0, 1000)
    ax3.tick_params(axis='y', labelcolor='g')

    # 光照
    ax4 = ax1.twinx()
    ax4.spines.right.set_position(("axes", 1.2))
    ax4.plot(time_data, df['lighting'], color=(1, 0.8, 0.4), label='Lighting', linewidth=1.2)
    ax4.set_ylabel('Lighting', color=(1, 0.8, 0.4))
    ax4.set_ylim(0, 2000)
    ax4.tick_params(axis='y', labelcolor=(1, 0.8, 0.4))

    # X轴 - 最近 15 天 (与 DuckDB 的提取周期呼应)
    end_date = df.index.max()
    start_date = end_date - pd.Timedelta(days=15)
    ax1.set_xlim([start_date, end_date])

    ax1.grid(True, linestyle='--', alpha=0.6)
    ax1.xaxis.set_major_locator(DayLocator(interval=1))
    ax1.xaxis.set_minor_locator(HourLocator(byhour=range(0, 24, 3)))
    ax1.xaxis.set_major_formatter(FuncFormatter(lambda x, _: mdates.num2date(x).strftime('%m-%d')))

    # 合并图例
    lines, labels = [], []
    for ax in [ax1, ax2, ax3, ax4]:
        l, lb = ax.get_legend_handles_labels()
        lines += l
        labels += lb
    ax1.legend(lines, labels, fontsize=10)

    plt.title(
        f"Env Data {title_suffix} ({df.index[-1].strftime('%Y-%m-%d %H:%M:%S')})",
        fontsize=20, pad=20
    )
    
    # 增加右侧留白，防止第四个Y轴(光照)被截断
    plt.subplots_adjust(right=0.8) 
    
    os.makedirs(STATIC_DIR, exist_ok=True)
    plt.savefig(plot_path)
    plt.close()
    print(f"✔ 图表已保存: {plot_path}")
    return True


# ===================== 4. 主入口 =====================
if __name__ == "__main__":
    print("开始批量绘图...")

    for cfg in PLOT_CONFIGS:
        df = load_and_process_data(cfg["data_file"])
        if not df.empty:
            plot_data(df, cfg["plot_path"], cfg["title_suffix"])
        print("-" * 30)

    print("全部完成 ✔")