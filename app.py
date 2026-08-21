from __future__ import annotations

import csv
import io
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd
import streamlit as st

from sim import APP_NAME
from sim.db import (
    all_rows,
    connect,
    current_round,
    database_bytes,
    employee_count,
    get_setting,
    hash_password,
    now_iso,
    one,
    reset_competition,
    restore_database_bytes,
    set_setting,
    settings_dict,
    setup_status,
    submission_status,
    verify_password,
)
from sim.defaults import GLOBAL_SETTING_LABELS, MARKET_COLUMNS
from sim.engine import market_size, settle_round


st.set_page_config(page_title=APP_NAME, page_icon="📈", layout="wide", initial_sidebar_state="expanded")
st.markdown(
    """
    <style>
    .stApp { background: #f6f8fb; }
    [data-testid="stSidebar"] { background: #10233f; }
    [data-testid="stSidebar"] * { color: #f3f7ff; }
    [data-testid="stMetric"] { background: white; border: 1px solid #e4e9f1; border-radius: 14px; padding: 14px; }
    div[data-testid="stForm"] { background: white; border: 1px solid #e4e9f1; border-radius: 16px; padding: 18px; }
    .hero { padding: 24px 26px; border-radius: 18px; color: white;
            background: linear-gradient(125deg,#12325c 0%,#155e75 58%,#0f766e 100%); margin-bottom: 18px; }
    .hero h1 { margin: 0 0 4px 0; font-size: 2rem; }
    .hero p { margin: 0; opacity: .88; }
    .hint { background:#eef7ff; border-left:4px solid #2c74b3; padding:12px 14px; border-radius:8px; }
    .danger { background:#fff1f2; border-left:4px solid #e11d48; padding:12px 14px; border-radius:8px; }
    </style>
    """,
    unsafe_allow_html=True,
)


STATUS_LABELS = {"waiting": "等待赛前设置", "open": "决策开放", "paused": "已暂停", "settled": "已结算"}


def money(value: float | int | None) -> str:
    return f"¥{float(value or 0):,.0f}"


def number(value: float | int | None) -> str:
    return f"{float(value or 0):,.0f}"


def percentage(value: float | int | None) -> str:
    return f"{float(value or 0) * 100:.2f}%"


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def secret_value(name: str, fallback: str) -> str:
    if os.environ.get(name):
        return str(os.environ[name])
    try:
        return str(st.secrets.get(name, fallback))
    except Exception:
        return fallback


def flash(level: str, message: str) -> None:
    st.session_state["flash"] = (level, message)


def show_flash() -> None:
    item = st.session_state.pop("flash", None)
    if not item:
        return
    level, message = item
    getattr(st, level, st.info)(message)


def hero(title: str, subtitle: str) -> None:
    st.markdown(f'<div class="hero"><h1>{title}</h1><p>{subtitle}</p></div>', unsafe_allow_html=True)


def rank_rows(conn: sqlite3.Connection, round_no: int | None = None) -> list[dict[str, Any]]:
    if round_no is None:
        latest = one(conn, "SELECT MAX(round_no) AS n FROM results")
        round_no = int(latest["n"] or 0) if latest else 0
    if not round_no:
        return []
    rows = all_rows(
        conn,
        "SELECT c.id,c.code,c.name,c.home_city,r.* FROM results r JOIN companies c ON c.id=r.company_id "
        "WHERE r.round_no=? ORDER BY r.net_assets DESC,c.id",
        (round_no,),
    )
    return [dict(row) | {"rank": index + 1} for index, row in enumerate(rows)]


def render_login() -> None:
    left, center, right = st.columns([1, 1.25, 1])
    with center:
        st.markdown("## 📈 阿思丹商赛模拟系统")
        st.caption("玩家决策 · 多城市 CPI 结算 · 管理员控制台")
        with st.form("login_form"):
            role_label = st.segmented_control("登录身份", ["玩家", "管理员"], default="玩家")
            account = st.text_input("账号", placeholder="例如 C01")
            password = st.text_input("密码", type="password")
            submitted = st.form_submit_button("登录", type="primary", use_container_width=True)
        if submitted:
            if role_label == "管理员":
                expected_user = secret_value("SIM_ADMIN_USER", "admin")
                expected_password = secret_value("SIM_ADMIN_PASSWORD", "admin123")
                if account == expected_user and password == expected_password:
                    st.session_state["auth"] = {"role": "admin"}
                    st.rerun()
                st.error("管理员账号或密码错误。")
            else:
                with connect() as conn:
                    company = one(conn, "SELECT * FROM companies WHERE code=?", (account.strip(),))
                    if company and verify_password(password, company["password_hash"]):
                        st.session_state["auth"] = {"role": "player", "company_id": int(company["id"])}
                        st.rerun()
                st.error("玩家账号或密码错误。")
        st.caption("初始玩家账号：C01–C04；初始密码：1234。部署前请在 Secrets 中修改管理员密码。")


def sidebar(role: str, company: sqlite3.Row | None = None) -> str:
    with st.sidebar:
        st.markdown("## 📊 商赛控制台")
        if role == "admin":
            st.caption("管理员")
            options = ["总览", "队伍管理", "KDS 设置", "回合控制", "赛后报表", "财富曲线", "备份与重置"]
        else:
            st.caption(f"{company['code']} · {company['name']}" if company else "玩家")
            options = ["概览", "本轮决策", "排行榜", "赛后报表", "财富曲线", "KDS"]
        page = st.radio("导航", options, label_visibility="collapsed")
        st.divider()
        if st.button("退出登录", use_container_width=True):
            st.session_state.clear()
            st.rerun()
    return page


def render_setup(company: sqlite3.Row) -> None:
    hero("赛前设置", "选择主场并提交公司名称；提交后由管理员统一开启第一轮。")
    if not company["home_city"]:
        with connect() as conn:
            markets = all_rows(conn, "SELECT * FROM market_config WHERE home_enabled=1 ORDER BY city")
        if not markets:
            st.error("管理员尚未配置可选主场。")
            return
        labels = [f"{m['city']}｜最高贷款 {money(m['max_loan'])}｜材料 {money(m['component_material'])}/{money(m['product_material'])}" for m in markets]
        with st.form("home_setup"):
            selected = st.selectbox("主场城市（确认后锁定）", labels)
            confirmed = st.form_submit_button("确认主场", type="primary")
        if confirmed:
            city = markets[labels.index(selected)]["city"]
            with connect() as conn:
                conn.execute("UPDATE companies SET home_city=?,setup_submitted_at=NULL WHERE id=?", (city, company["id"]))
                conn.execute(
                    "INSERT INTO agents(company_id,city,count) VALUES(?,?,1) "
                    "ON CONFLICT(company_id,city) DO UPDATE SET count=MAX(count,1)",
                    (company["id"], city),
                )
            flash("success", f"主场已锁定为 {city}。")
            st.rerun()
        return

    st.success(f"已锁定主场：{company['home_city']}")
    if not company["setup_submitted_at"]:
        with st.form("name_setup"):
            name = st.text_input("公司名称", value="" if str(company["name"]).startswith("待命名-") else company["name"], max_chars=40)
            confirmed = st.form_submit_button("提交并进入等待区", type="primary")
        if confirmed:
            clean_name = name.strip()
            if len(clean_name) < 2:
                st.error("公司名称至少需要 2 个字符。")
                return
            with connect() as conn:
                duplicate = one(conn, "SELECT id FROM companies WHERE lower(name)=lower(?) AND id<>?", (clean_name, company["id"]))
                if duplicate:
                    st.error("公司名称已被其他队伍使用。")
                    return
                conn.execute(
                    "UPDATE companies SET name=?,setup_submitted_at=? WHERE id=?",
                    (clean_name, now_iso(), company["id"]),
                )
            flash("success", "赛前设置已提交。")
            st.rerun()


def round_banner(round_row: sqlite3.Row | None) -> None:
    if not round_row:
        st.info("管理员尚未创建回合。")
        return
    cols = st.columns(3)
    cols[0].metric("当前轮次", f"第 {round_row['round_no']} 轮")
    cols[1].metric("状态", STATUS_LABELS.get(round_row["status"], round_row["status"]))
    end = parse_time(round_row["ends_at"])
    if end and round_row["status"] == "open":
        remaining = end - datetime.now(timezone.utc)
        seconds = max(0, int(remaining.total_seconds()))
        cols[2].metric("剩余时间", f"{seconds // 60:02d}:{seconds % 60:02d}")
    else:
        cols[2].metric("截止时间", end.astimezone().strftime("%H:%M:%S") if end else "—")


def render_player_overview(company: sqlite3.Row) -> None:
    hero(f"你好，{company['name']}", "以 Net Cash（现金减负债）为核心，平衡生产、价格、投资和库存风险。")
    with connect() as conn:
        round_row = current_round(conn)
        latest = one(conn, "SELECT * FROM results WHERE company_id=? ORDER BY round_no DESC LIMIT 1", (company["id"],))
        ranking = rank_rows(conn, int(latest["round_no"]) if latest else 0)
        my_rank = next((row["rank"] for row in ranking if row["id"] == company["id"]), None)
        workers = employee_count(conn, company["id"], "worker")
        engineers = employee_count(conn, company["id"], "engineer")
        ready = setup_status(conn)
    round_banner(round_row)
    cols = st.columns(5)
    cols[0].metric("现金", money(company["cash"]))
    cols[1].metric("负债", money(company["debt"]))
    cols[2].metric("库存", number(company["product_inventory"]))
    cols[3].metric("员工", f"{workers + engineers:,}")
    cols[4].metric("最新排名", f"#{my_rank}" if my_rank else "—")
    if round_row and round_row["status"] == "waiting":
        st.info(f"已有 {ready['ready']}/{ready['total']} 支队伍完成赛前设置。全部就绪后管理员才能开始第一轮。")
    if latest:
        st.subheader("上一轮摘要")
        summary = pd.DataFrame(
            [{
                "轮次": int(latest["round_no"]),
                "生产": int(latest["produced"]),
                "售出": int(latest["sold"]),
                "库存": int(latest["inventory"]),
                "净利润": money(latest["net_profit"]),
                "净现金": money(latest["net_assets"]),
            }]
        )
        st.dataframe(summary, hide_index=True, use_container_width=True)


def decision_helper(conn: sqlite3.Connection, company: sqlite3.Row, round_no: int) -> dict[str, Any]:
    previous = all_rows(
        conn,
        "SELECT worker_salary,engineer_salary FROM decisions WHERE round_no=? AND submitted_at IS NOT NULL",
        (round_no - 1,),
    ) if round_no > 1 else []
    home = one(conn, "SELECT * FROM market_config WHERE city=?", (company["home_city"],))
    if previous:
        avg_worker = sum(float(row["worker_salary"]) for row in previous) / len(previous)
        avg_engineer = sum(float(row["engineer_salary"]) for row in previous) / len(previous)
    else:
        avg_worker = float(home["worker_initial_salary"])
        avg_engineer = float(home["engineer_initial_salary"])
    salary_max = get_setting(conn, "salary_max", 10_000.0)
    research = get_setting(conn, "research_75", 6_000_000.0) / 0.75 * 1.10 + get_setting(conn, "research_buffer", 150_000.0)
    return {
        "worker_wage": min(salary_max, (avg_worker + 100) * 1.10),
        "engineer_wage": min(salary_max, (avg_engineer + 100) * 1.10),
        "research": research,
    }


def render_player_decision(company: sqlite3.Row) -> None:
    hero("本轮决策", "决策可以在截止前重复提交；系统只保留最后一次提交。")
    with connect() as conn:
        round_row = current_round(conn)
        if not round_row:
            st.info("暂无回合。")
            return
        round_banner(round_row)
        if round_row["status"] != "open":
            st.warning("当前回合未开放决策。")
            return
        end = parse_time(round_row["ends_at"])
        if end and datetime.now(timezone.utc) > end:
            st.error("本轮提交时间已结束，请等待管理员结算。")
            return
        round_no = int(round_row["round_no"])
        markets = all_rows(conn, "SELECT * FROM market_config ORDER BY city")
        home = one(conn, "SELECT * FROM market_config WHERE city=?", (company["home_city"],))
        current_workers = employee_count(conn, company["id"], "worker")
        current_engineers = employee_count(conn, company["id"], "engineer")
        decision_row = one(conn, "SELECT * FROM decisions WHERE company_id=? AND round_no=?", (company["id"], round_no))
        decision = dict(decision_row) if decision_row else {
            "loan_change": 0.0,
            "worker_delta": 0,
            "worker_salary": float(home["worker_initial_salary"]),
            "engineer_delta": 0,
            "engineer_salary": float(home["engineer_initial_salary"]),
            "management_investment": 0.0,
            "production_volume": 0,
            "quality_investment": 0.0,
            "research_investment": 0.0,
            "submitted_at": None,
        }
        city_values: dict[str, dict[str, Any]] = {}
        for market in markets:
            city = str(market["city"])
            saved = one(conn, "SELECT * FROM city_decisions WHERE company_id=? AND round_no=? AND city=?", (company["id"], round_no, city))
            agent = one(conn, "SELECT count FROM agents WHERE company_id=? AND city=?", (company["id"], city))
            city_values[city] = dict(saved) if saved else {
                "agent_delta": 0,
                "marketing_investment": 0.0,
                "price": float(market["initial_avg_price"]),
                "order_report": 0,
            }
            city_values[city]["current_agents"] = int(agent["count"]) if agent else 0
        helper = decision_helper(conn, company, round_no)
        settings = settings_dict(conn)

    st.markdown(
        f'<div class="hint">工资建议：工人约 <b>{money(helper["worker_wage"])}</b>，工程师约 '
        f'<b>{money(helper["engineer_wage"])}</b>；75% 专利参考投入约 <b>{money(helper["research"])}</b>。</div>',
        unsafe_allow_html=True,
    )
    if decision.get("submitted_at"):
        st.success(f"已提交；最后保存时间：{str(decision['submitted_at'])[:19].replace('T', ' ')} UTC")

    with st.form(f"decision_{round_no}"):
        st.subheader("经营与生产")
        col1, col2, col3 = st.columns(3)
        loan_min = -float(company["debt"])
        loan_max = max(0.0, float(home["max_loan"]) - float(company["debt"]))
        loan_change = col1.number_input("贷款变化（负数为还款）", min_value=loan_min, max_value=loan_max, value=float(decision["loan_change"]), step=10_000.0)
        production_volume = col2.number_input("计划生产量", min_value=0, value=int(decision["production_volume"]), step=1)
        management = col3.number_input("Management Investment", min_value=0.0, value=float(decision["management_investment"]), step=10_000.0)

        col1, col2, col3 = st.columns(3)
        worker_delta = col1.number_input("工人增减", min_value=-current_workers, value=int(decision["worker_delta"]), step=1)
        worker_salary = col2.number_input(
            "工人月薪",
            min_value=float(settings["salary_min"]),
            max_value=float(settings["salary_max"]),
            value=float(decision["worker_salary"]),
            step=50.0,
        )
        col3.caption(f"当前工人：{current_workers:,}；新员工培训费按主场 KDS 一次性计收。")

        col1, col2, col3 = st.columns(3)
        engineer_delta = col1.number_input("工程师增减", min_value=-current_engineers, value=int(decision["engineer_delta"]), step=1)
        engineer_salary = col2.number_input(
            "工程师月薪",
            min_value=float(settings["salary_min"]),
            max_value=float(settings["salary_max"]),
            value=float(decision["engineer_salary"]),
            step=50.0,
        )
        col3.caption(f"当前工程师：{current_engineers:,}；第三轮起对应老员工享受 1.1 经验倍率。")

        col1, col2 = st.columns(2)
        quality = col1.number_input("Quality Investment", min_value=0.0, value=float(decision["quality_investment"]), step=10_000.0)
        research = col2.number_input("R&D / 专利投入", min_value=0.0, value=float(decision["research_investment"]), step=10_000.0)

        st.subheader("城市销售")
        city_inputs: dict[str, dict[str, Any]] = {}
        for market in markets:
            city = str(market["city"])
            values = city_values[city]
            with st.expander(f"{city} · 当前 Agent {values['current_agents']} · 市场容量本轮约 {number(market_size(market, round_no, float(settings['market_growth'])))}"):
                c1, c2, c3, c4 = st.columns(4)
                agent_delta = c1.number_input(
                    "Agent 增减",
                    min_value=-int(values["current_agents"]),
                    max_value=int(settings["max_agent_add_per_round"]),
                    value=int(values["agent_delta"]),
                    step=1,
                    key=f"agent_{round_no}_{city}",
                )
                marketing = c2.number_input(
                    "Marketing Investment",
                    min_value=0.0,
                    value=float(values["marketing_investment"]),
                    step=10_000.0,
                    key=f"mi_{round_no}_{city}",
                )
                city_max = min(float(settings["price_max"]), float(market["max_price"]))
                saved_price = min(max(float(values["price"]), float(settings["price_min"])), city_max)
                price = c3.number_input(
                    "售价",
                    min_value=float(settings["price_min"]),
                    max_value=city_max,
                    value=saved_price,
                    step=50.0,
                    key=f"price_{round_no}_{city}",
                )
                order_report = c4.checkbox("购买市场报告", value=bool(values["order_report"]), key=f"report_{round_no}_{city}")
                city_inputs[city] = {
                    "agent_delta": int(agent_delta),
                    "marketing_investment": float(marketing),
                    "price": float(price),
                    "order_report": int(order_report),
                    "current_agents": int(values["current_agents"]),
                }
        submitted = st.form_submit_button("提交本轮决策", type="primary", use_container_width=True)

    if submitted:
        total_agent_add = sum(max(0, row["agent_delta"]) for row in city_inputs.values())
        errors: list[str] = []
        if total_agent_add > int(settings["max_agent_add_per_round"]):
            errors.append(f"本轮最多新增 {int(settings['max_agent_add_per_round'])} 个 Agent，当前填写 {total_agent_add} 个。")
        for city, values in city_inputs.items():
            if values["current_agents"] + values["agent_delta"] < 0:
                errors.append(f"{city} 的 Agent 数量不能为负数。")
        if errors:
            for error in errors:
                st.error(error)
            return
        with connect() as conn:
            latest_round = current_round(conn)
            latest_end = parse_time(latest_round["ends_at"]) if latest_round else None
            if not latest_round or latest_round["status"] != "open" or int(latest_round["round_no"]) != round_no or (latest_end and datetime.now(timezone.utc) > latest_end):
                st.error("回合状态已经变化，本次提交未保存。")
                return
            conn.execute(
                "INSERT INTO decisions(company_id,round_no,loan_change,worker_delta,worker_salary,engineer_delta,engineer_salary,"
                "management_investment,production_volume,quality_investment,research_investment,submitted_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(company_id,round_no) DO UPDATE SET loan_change=excluded.loan_change,worker_delta=excluded.worker_delta,"
                "worker_salary=excluded.worker_salary,engineer_delta=excluded.engineer_delta,engineer_salary=excluded.engineer_salary,"
                "management_investment=excluded.management_investment,production_volume=excluded.production_volume,"
                "quality_investment=excluded.quality_investment,research_investment=excluded.research_investment,submitted_at=excluded.submitted_at",
                (
                    company["id"], round_no, loan_change, int(worker_delta), worker_salary, int(engineer_delta), engineer_salary,
                    management, int(production_volume), quality, research, now_iso(),
                ),
            )
            for city, values in city_inputs.items():
                conn.execute(
                    "INSERT INTO city_decisions(company_id,round_no,city,agent_delta,marketing_investment,price,order_report) "
                    "VALUES(?,?,?,?,?,?,?) ON CONFLICT(company_id,round_no,city) DO UPDATE SET agent_delta=excluded.agent_delta,"
                    "marketing_investment=excluded.marketing_investment,price=excluded.price,order_report=excluded.order_report",
                    (company["id"], round_no, city, values["agent_delta"], values["marketing_investment"], values["price"], values["order_report"]),
                )
        flash("success", "本轮决策已保存。")
        st.rerun()


def ranking_table(rows: list[dict[str, Any]]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "排名": row["rank"],
                "队伍": row["code"],
                "公司": row["name"],
                "主场": row["home_city"],
                "净现金": row["net_assets"],
                "现金": row["cash"],
                "本轮利润": row["net_profit"],
                "售出": row["sold"],
                "库存": row["inventory"],
            }
            for row in rows
        ]
    )


def render_ranking(admin: bool = False) -> None:
    hero("财富排行榜", "按 Net Cash 排序；Net Cash = 期末现金 − 负债，未售库存不计入排名。")
    with connect() as conn:
        latest = one(conn, "SELECT MAX(round_no) AS n FROM results")
        latest_round = int(latest["n"] or 0) if latest else 0
        if not latest_round:
            st.info("暂无已结算回合。")
            return
        round_numbers = [int(row["round_no"]) for row in all_rows(conn, "SELECT DISTINCT round_no FROM results ORDER BY round_no DESC")]
        selected = st.selectbox("选择轮次", round_numbers, index=0)
        rows = rank_rows(conn, selected)
    frame = ranking_table(rows)
    st.dataframe(
        frame,
        hide_index=True,
        use_container_width=True,
        column_config={
            "净现金": st.column_config.NumberColumn(format="¥ %.0f"),
            "现金": st.column_config.NumberColumn(format="¥ %.0f"),
            "本轮利润": st.column_config.NumberColumn(format="¥ %.0f"),
        },
    )


def report_csv(report: dict[str, Any]) -> bytes:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Section", "Item", "Value"])
    for section, value in report.items():
        if isinstance(value, dict):
            for key, item in value.items():
                writer.writerow([section, key, json.dumps(item, ensure_ascii=False) if isinstance(item, (dict, list)) else item])
    return output.getvalue().encode("utf-8-sig")


def render_report_detail(conn: sqlite3.Connection, company_id: int, round_no: int, admin: bool) -> None:
    company = one(conn, "SELECT * FROM companies WHERE id=?", (company_id,))
    row = one(conn, "SELECT * FROM results WHERE company_id=? AND round_no=?", (company_id, round_no))
    if not company or not row:
        st.error("未找到报表。")
        return
    report = json.loads(row["report_json"])
    st.subheader(f"{company['name']} · 第 {round_no} 轮")
    metrics = report["key_metrics"]
    cols = st.columns(5)
    cols[0].metric("净现金", money(metrics["net_assets"]))
    cols[1].metric("现金", money(row["cash"]))
    cols[2].metric("销售收入", money(metrics["sales_revenue"]))
    cols[3].metric("净利润", money(metrics["net_profit"]))
    cols[4].metric("库存", number(row["inventory"]))

    tab1, tab2, tab3, tab4 = st.tabs(["财务", "生产与人力", "城市销售 / CPI", "专利"])
    with tab1:
        finance_labels = {
            "round_begins": "期初现金", "loan_change": "贷款变化", "wages": "工资", "layoff": "裁员费",
            "training": "培训费", "materials": "材料", "storage": "仓储扩容", "agents": "Agent",
            "marketing": "MI", "management": "MA", "quality": "QI", "market_reports": "报告",
            "research": "专利投入", "interest": "利息", "tax": "税", "round_ends": "期末现金",
        }
        finance = pd.DataFrame([{"项目": finance_labels.get(key, key), "金额": value} for key, value in report["finance"].items()])
        st.dataframe(finance, hide_index=True, use_container_width=True, column_config={"金额": st.column_config.NumberColumn(format="¥ %.0f")})
    with tab2:
        production = report["production"]
        hr = report["human_resources"]
        left, right = st.columns(2)
        left.dataframe(pd.DataFrame([{"指标": key, "数值": value} for key, value in production.items()]), hide_index=True, use_container_width=True)
        right.dataframe(pd.DataFrame([{"指标": key, "数值": value} for key, value in hr.items()]), hide_index=True, use_container_width=True)
    with tab3:
        sales_frame = pd.DataFrame(
            [
                {
                    "城市": item["city"], "Agent": item["agents"], "售价": item["price"], "MI": item["marketing"],
                    "CPI%": item["cpi"], "CPI理论量": item["cpi_units"], "二次分配": item["secondary_units"],
                    "售出": item["sold"], "市场份额%": item["market_share"] * 100, "市场容量": item["market_size"],
                }
                for item in report["sales"]
            ]
        )
        st.dataframe(
            sales_frame,
            hide_index=True,
            use_container_width=True,
            column_config={"市场份额%": st.column_config.NumberColumn(format="%.2f%%"), "售价": st.column_config.NumberColumn(format="¥ %.0f"), "MI": st.column_config.NumberColumn(format="¥ %.0f")},
        )
        visible_cities: set[str]
        if admin:
            visible_cities = {str(row["city"]) for row in all_rows(conn, "SELECT city FROM market_config")}
        else:
            visible_cities = {
                str(item["city"])
                for item in all_rows(
                    conn,
                    "SELECT city FROM city_decisions WHERE company_id=? AND round_no=? AND order_report=1",
                    (company_id, round_no),
                )
            }
        if visible_cities:
            st.markdown("#### 市场报告")
            for city in sorted(visible_cities):
                rows = all_rows(
                    conn,
                    "SELECT c.name,cr.*,r.ma_index,r.qi_index FROM city_results cr JOIN companies c ON c.id=cr.company_id "
                    "JOIN results r ON r.company_id=cr.company_id AND r.round_no=cr.round_no "
                    "WHERE cr.round_no=? AND cr.city=? ORDER BY cr.market_share DESC",
                    (round_no, city),
                )
                market_frame = pd.DataFrame(
                    [{"公司": r["name"], "价格": r["price"], "MA指数": r["ma_index"], "QI指数": r["qi_index"], "MI": r["marketing"], "CPI%": r["cpi"], "售出": r["sold"], "市场份额%": r["market_share"] * 100} for r in rows]
                )
                with st.expander(city):
                    st.dataframe(market_frame, hide_index=True, use_container_width=True, column_config={"市场份额%": st.column_config.NumberColumn(format="%.2f%%"), "价格": st.column_config.NumberColumn(format="¥ %.0f"), "MI": st.column_config.NumberColumn(format="¥ %.0f")})
    with tab4:
        research = report["research"]
        st.write(f"投入：{money(research['investment'])}")
        st.write(f"成功概率：{percentage(research['probability'])}")
        st.write("结果：" + ("✅ 专利成功" if research["success"] else "❌ 未获得专利"))
        st.write(f"累计专利：{research['patents_after']}")
    st.download_button("下载本轮 CSV", report_csv(report), file_name=f"round_{round_no}_{company['code']}.csv", mime="text/csv")


def render_reports(company: sqlite3.Row | None, admin: bool = False) -> None:
    hero("赛后报表", "查看现金流、生产、人力、城市 CPI 分解和专利结果。")
    with connect() as conn:
        if admin:
            companies = all_rows(conn, "SELECT * FROM companies ORDER BY code")
            if not companies:
                st.info("暂无队伍。")
                return
            labels = [f"{row['code']} · {row['name']}" for row in companies]
            selected_label = st.selectbox("队伍", labels)
            selected_company = companies[labels.index(selected_label)]
        else:
            selected_company = company
        rounds = all_rows(conn, "SELECT round_no FROM results WHERE company_id=? ORDER BY round_no DESC", (selected_company["id"],))
        if not rounds:
            st.info("暂无已结算报表。")
            return
        round_no = st.selectbox("轮次", [int(row["round_no"]) for row in rounds])
        render_report_detail(conn, int(selected_company["id"]), int(round_no), admin)


def render_wealth(company: sqlite3.Row | None, admin: bool = False) -> None:
    hero("财富曲线", "跟踪各轮 Net Cash（期末现金 − 负债）变化。")
    with connect() as conn:
        if admin:
            rows = all_rows(
                conn,
                "SELECT c.name,r.round_no,r.net_assets FROM results r JOIN companies c ON c.id=r.company_id ORDER BY r.round_no,c.id",
            )
        else:
            rows = all_rows(conn, "SELECT ? AS name,round_no,net_assets FROM results WHERE company_id=? ORDER BY round_no", (company["name"], company["id"]))
    if not rows:
        st.info("暂无已结算数据。")
        return
    frame = pd.DataFrame([dict(row) for row in rows])
    pivot = frame.pivot(index="round_no", columns="name", values="net_assets")
    st.line_chart(pivot, x_label="轮次", y_label="净现金")
    st.dataframe(frame.rename(columns={"name": "公司", "round_no": "轮次", "net_assets": "净现金"}), hide_index=True, use_container_width=True, column_config={"净现金": st.column_config.NumberColumn(format="¥ %.0f")})


def render_player_kds(company: sqlite3.Row) -> None:
    hero("KDS 与公式", "当前比赛参数只读；管理员可在后台统一修改。")
    with connect() as conn:
        settings = settings_dict(conn)
        markets = all_rows(conn, "SELECT * FROM market_config ORDER BY city")
    st.markdown(
        f"""
        - 每轮工时：`504`
        - 工人 : 工程师 = `A × B × E : C × D`
        - 工资产能倍率：`min(本队工资 ÷ 本轮平均工资, 1.1)`
        - MA 指数：`MA 投资 ÷ (工人 + 工程师)`
        - QI 指数：`QI 投资 ÷ (旧产品 × 1.2 + 新产品)`
        - QI 大量 CPI 门槛：`城市最高价 ÷ 50`
        - CPI：按城市独立执行赠品、第一层、第二层、福利 1/2；价格差使用 `{int(settings['cpi_price_power'])}` 次方。
        """
    )
    setting_frame = pd.DataFrame([{"参数": GLOBAL_SETTING_LABELS.get(key, key), "值": value} for key, value in settings.items() if key in GLOBAL_SETTING_LABELS])
    st.dataframe(setting_frame, hide_index=True, use_container_width=True)
    market_frame = pd.DataFrame([dict(row) for row in markets]).rename(columns=MARKET_COLUMNS)
    st.dataframe(market_frame, hide_index=True, use_container_width=True)


def render_admin_overview() -> None:
    hero("管理员总览", "统一管理队伍、KDS、回合结算与数据备份。")
    with connect() as conn:
        round_row = current_round(conn)
        companies = all_rows(conn, "SELECT * FROM companies ORDER BY id")
        setup = setup_status(conn)
        submission = submission_status(conn, int(round_row["round_no"])) if round_row and round_row["status"] in ("open", "paused") else None
        ranking = rank_rows(conn)
    round_banner(round_row)
    cols = st.columns(4)
    cols[0].metric("队伍数", len(companies))
    cols[1].metric("赛前就绪", f"{setup['ready']}/{setup['total']}")
    cols[2].metric("本轮已提交", f"{submission['submitted']}/{submission['total']}" if submission else "—")
    cols[3].metric("已结算轮次", max((int(row["round_no"]) for row in ranking), default=0))
    if ranking:
        st.subheader("最新排名")
        st.dataframe(ranking_table(ranking), hide_index=True, use_container_width=True, column_config={"净现金": st.column_config.NumberColumn(format="¥ %.0f"), "现金": st.column_config.NumberColumn(format="¥ %.0f"), "本轮利润": st.column_config.NumberColumn(format="¥ %.0f")})


def render_admin_companies() -> None:
    hero("队伍管理", "新增队伍、重置玩家密码或解锁赛前设置。")
    with st.form("add_company", clear_on_submit=True):
        c1, c2, c3 = st.columns(3)
        code = c1.text_input("队伍账号", placeholder="C05")
        name = c2.text_input("初始名称", placeholder="待命名-C05")
        password = c3.text_input("初始密码", value="1234", type="password")
        add = st.form_submit_button("新增队伍", type="primary")
    if add:
        clean_code = code.strip().upper()
        if not clean_code or not password:
            st.error("账号和密码不能为空。")
        else:
            try:
                with connect() as conn:
                    conn.execute(
                        "INSERT INTO companies(code,name,password_hash,cash,created_at) VALUES(?,?,?,?,?)",
                        (clean_code, name.strip() or f"待命名-{clean_code}", hash_password(password), get_setting(conn, "initial_cash", 15_000_000.0), now_iso()),
                    )
                flash("success", f"已新增队伍 {clean_code}。")
                st.rerun()
            except sqlite3.IntegrityError:
                st.error("账号或公司名称重复。")

    with connect() as conn:
        companies = all_rows(conn, "SELECT * FROM companies ORDER BY id")
    for company in companies:
        with st.expander(f"{company['code']} · {company['name']} · {company['home_city'] or '未选主场'}"):
            cols = st.columns([2, 1, 1])
            new_password = cols[0].text_input("新密码", type="password", key=f"pw_{company['id']}")
            if cols[1].button("重置密码", key=f"resetpw_{company['id']}", use_container_width=True):
                if not new_password:
                    st.error("请先输入新密码。")
                else:
                    with connect() as conn:
                        conn.execute("UPDATE companies SET password_hash=? WHERE id=?", (hash_password(new_password), company["id"]))
                    flash("success", f"{company['code']} 密码已重置。")
                    st.rerun()
            if cols[2].button("解锁赛前设置", key=f"unlock_{company['id']}", use_container_width=True):
                with connect() as conn:
                    conn.execute("UPDATE companies SET home_city=NULL,setup_submitted_at=NULL WHERE id=?", (company["id"],))
                    conn.execute("DELETE FROM agents WHERE company_id=?", (company["id"],))
                flash("success", f"{company['code']} 已解锁。")
                st.rerun()
            st.caption(f"现金 {money(company['cash'])} · 负债 {money(company['debt'])} · 专利 {company['patents']} · 库存 {company['product_inventory']}")


def render_admin_kds() -> None:
    hero("KDS 设置", "修改将影响后续结算；已结算结果不会追溯变化。")
    with connect() as conn:
        settings = settings_dict(conn)
        markets = all_rows(conn, "SELECT * FROM market_config ORDER BY city")
    with st.form("global_kds"):
        values: dict[str, Any] = {}
        items = [key for key in GLOBAL_SETTING_LABELS if key in settings]
        for start in range(0, len(items), 3):
            cols = st.columns(3)
            for offset, key in enumerate(items[start:start + 3]):
                default = settings[key]
                if isinstance(default, int):
                    values[key] = cols[offset].number_input(GLOBAL_SETTING_LABELS[key], value=int(default), step=1, key=f"setting_{key}")
                else:
                    values[key] = cols[offset].number_input(GLOBAL_SETTING_LABELS[key], value=float(default), step=0.01 if abs(float(default)) < 2 else 100.0, format="%.4f" if abs(float(default)) < 2 else "%.2f", key=f"setting_{key}")
        save = st.form_submit_button("保存全局 KDS", type="primary")
    if save:
        if values["salary_min"] > values["salary_max"] or values["price_min"] > values["price_max"]:
            st.error("最低值不能高于最高值。")
        else:
            with connect() as conn:
                for key, value in values.items():
                    set_setting(conn, key, value)
            flash("success", "全局 KDS 已保存。")
            st.rerun()

    st.subheader("城市参数")
    frame = pd.DataFrame([dict(row) for row in markets])
    edited = st.data_editor(
        frame,
        hide_index=True,
        use_container_width=True,
        disabled=["city"],
        column_config={
            key: (st.column_config.CheckboxColumn(label) if key == "home_enabled" else st.column_config.Column(label))
            for key, label in MARKET_COLUMNS.items()
        },
        key="market_editor",
    )
    if st.button("保存全部城市参数", type="primary"):
        numeric_columns = [column for column in frame.columns if column not in ("city", "home_enabled")]
        try:
            with connect() as conn:
                for record in edited.to_dict("records"):
                    values_sql = [int(bool(record["home_enabled"]))] + [float(record[column]) for column in numeric_columns]
                    assignments = ["home_enabled=?"] + [f"{column}=?" for column in numeric_columns]
                    conn.execute(f"UPDATE market_config SET {','.join(assignments)} WHERE city=?", (*values_sql, record["city"]))
            flash("success", "城市 KDS 已保存。")
            st.rerun()
        except (TypeError, ValueError):
            st.error("城市参数必须是有效数字。")

    with st.form("add_city", clear_on_submit=True):
        city = st.text_input("新增城市名称")
        add_city = st.form_submit_button("新增城市")
    if add_city and city.strip():
        try:
            with connect() as conn:
                conn.execute(
                    "INSERT INTO market_config(city,home_enabled,max_loan,interest_rate,worker_initial_salary,engineer_initial_salary,"
                    "component_material,product_material,component_storage,product_storage,population,penetration,initial_avg_price,max_price,"
                    "transport_cost,worker_training_cost,engineer_training_cost) VALUES(?,1,0,0,0,0,0,0,0,0,1,0.01,0,?,0,0,0)",
                    (city.strip(), get_setting(conn, "price_max", 25_000.0)),
                )
            flash("success", f"已新增城市 {city.strip()}，请补充参数。")
            st.rerun()
        except sqlite3.IntegrityError:
            st.error("城市名称重复。")


def render_admin_rounds() -> None:
    hero("回合控制", "第一轮需全部队伍完成赛前设置；每轮需全部队伍提交后才能结算。")
    with connect() as conn:
        round_row = current_round(conn)
        setup = setup_status(conn)
        submission = submission_status(conn, int(round_row["round_no"])) if round_row and round_row["status"] in ("open", "paused") else None
        decisions = all_rows(
            conn,
            "SELECT c.code,c.name,c.home_city,c.setup_submitted_at,d.submitted_at,d.production_volume,d.management_investment,d.quality_investment,d.research_investment "
            "FROM companies c LEFT JOIN decisions d ON d.company_id=c.id AND d.round_no=? ORDER BY c.id",
            (int(round_row["round_no"]),),
        ) if round_row else []
        history = all_rows(conn, "SELECT * FROM rounds ORDER BY round_no DESC")
    round_banner(round_row)
    st.write(f"赛前就绪：{setup['ready']}/{setup['total']}")
    if submission:
        st.write(f"本轮提交：{submission['submitted']}/{submission['total']}")

    if round_row and round_row["status"] == "waiting":
        minutes = st.number_input("第一轮时长（分钟）", min_value=1, value=30, step=1)
        if st.button("开始第一轮", type="primary", disabled=not bool(setup["all_ready"])):
            start = datetime.now(timezone.utc)
            with connect() as conn:
                conn.execute("UPDATE rounds SET status='open',starts_at=?,ends_at=? WHERE round_no=?", (start.isoformat(), (start + timedelta(minutes=int(minutes))).isoformat(), round_row["round_no"]))
            flash("success", "第一轮已开始。")
            st.rerun()
    elif round_row and round_row["status"] in ("open", "paused"):
        cols = st.columns(4)
        if round_row["status"] == "open":
            if cols[0].button("暂停", use_container_width=True):
                with connect() as conn:
                    conn.execute("UPDATE rounds SET status='paused' WHERE round_no=?", (round_row["round_no"],))
                st.rerun()
        else:
            if cols[0].button("继续", use_container_width=True):
                with connect() as conn:
                    conn.execute("UPDATE rounds SET status='open' WHERE round_no=?", (round_row["round_no"],))
                st.rerun()
        extend_minutes = cols[1].number_input("延长分钟", min_value=1, value=5, step=1, label_visibility="collapsed")
        if cols[2].button("延长", use_container_width=True):
            old_end = parse_time(round_row["ends_at"]) or datetime.now(timezone.utc)
            with connect() as conn:
                conn.execute("UPDATE rounds SET ends_at=? WHERE round_no=?", ((old_end + timedelta(minutes=int(extend_minutes))).isoformat(), round_row["round_no"]))
            flash("success", f"已延长 {extend_minutes} 分钟。")
            st.rerun()
        if cols[3].button("结算本轮", type="primary", use_container_width=True, disabled=not bool(submission and submission["all_submitted"])):
            try:
                with connect() as conn:
                    settle_round(conn, int(round_row["round_no"]))
                flash("success", f"第 {round_row['round_no']} 轮结算完成。")
                st.rerun()
            except Exception as exc:
                st.error(f"结算失败：{exc}")
    elif round_row and round_row["status"] == "settled":
        minutes = st.number_input("下一轮时长（分钟）", min_value=1, value=30, step=1)
        if st.button("开启下一轮", type="primary"):
            start = datetime.now(timezone.utc)
            next_round = int(round_row["round_no"]) + 1
            with connect() as conn:
                conn.execute("INSERT INTO rounds(round_no,status,starts_at,ends_at) VALUES(?,'open',?,?)", (next_round, start.isoformat(), (start + timedelta(minutes=int(minutes))).isoformat()))
            flash("success", f"第 {next_round} 轮已开始。")
            st.rerun()

    if decisions:
        st.subheader("队伍状态")
        status_frame = pd.DataFrame(
            [{"队伍": row["code"], "公司": row["name"], "主场": row["home_city"] or "—", "赛前就绪": bool(row["setup_submitted_at"]), "本轮提交": bool(row["submitted_at"]), "计划产量": row["production_volume"] or 0, "MA": row["management_investment"] or 0, "QI": row["quality_investment"] or 0, "专利": row["research_investment"] or 0} for row in decisions]
        )
        st.dataframe(status_frame, hide_index=True, use_container_width=True)
    st.subheader("回合历史")
    st.dataframe(pd.DataFrame([dict(row) for row in history]), hide_index=True, use_container_width=True)


def render_backup_reset() -> None:
    hero("备份与重置", "SQLite 数据在部分云平台重启后可能丢失；建议每轮结算后下载备份。")
    backup = database_bytes()
    st.download_button("下载完整数据库备份", backup, file_name=f"business_sim_{datetime.now().strftime('%Y%m%d_%H%M')}.db", mime="application/x-sqlite3", disabled=not bool(backup))
    st.subheader("恢复备份")
    uploaded = st.file_uploader("上传本系统导出的 .db 文件", type=["db", "sqlite", "sqlite3"])
    restore_confirm = st.text_input("输入 RESTORE 确认覆盖当前数据")
    if st.button("恢复数据库", disabled=uploaded is None or restore_confirm != "RESTORE"):
        try:
            restore_database_bytes(uploaded.getvalue())
            st.session_state.clear()
            st.success("恢复完成，请重新登录。")
            st.rerun()
        except Exception as exc:
            st.error(f"恢复失败：{exc}")
    st.divider()
    st.subheader("重置整场比赛")
    st.markdown('<div class="danger">这会清除全部回合、决策、报表、员工和 Agent 数据，但保留队伍账号与 KDS。</div>', unsafe_allow_html=True)
    confirm = st.text_input("输入 RESET 确认")
    if st.button("重置比赛", type="primary", disabled=confirm != "RESET"):
        with connect() as conn:
            reset_competition(conn)
        flash("success", "比赛已重置。")
        st.rerun()


def main() -> None:
    show_flash()
    auth = st.session_state.get("auth")
    if not auth:
        render_login()
        return
    if auth.get("role") == "admin":
        page = sidebar("admin")
        {
            "总览": render_admin_overview,
            "队伍管理": render_admin_companies,
            "KDS 设置": render_admin_kds,
            "回合控制": render_admin_rounds,
            "赛后报表": lambda: render_reports(None, admin=True),
            "财富曲线": lambda: render_wealth(None, admin=True),
            "备份与重置": render_backup_reset,
        }[page]()
        return

    company_id = int(auth.get("company_id", 0))
    with connect() as conn:
        company = one(conn, "SELECT * FROM companies WHERE id=?", (company_id,))
    if not company:
        st.session_state.clear()
        st.rerun()
    if not company["home_city"] or not company["setup_submitted_at"]:
        sidebar("player", company)
        render_setup(company)
        return
    page = sidebar("player", company)
    {
        "概览": lambda: render_player_overview(company),
        "本轮决策": lambda: render_player_decision(company),
        "排行榜": render_ranking,
        "赛后报表": lambda: render_reports(company),
        "财富曲线": lambda: render_wealth(company),
        "KDS": lambda: render_player_kds(company),
    }[page]()


if __name__ == "__main__":
    main()
