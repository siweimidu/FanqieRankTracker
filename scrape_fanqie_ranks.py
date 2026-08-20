"""
番茄榜单爬虫 —— 多榜单版。

遍历 boards_config.BOARDS 中所有 enabled 榜单，逐个：
  1. 打开榜单 init_url
  2. 自动发现页面内的分类目录（/rank/ 同前缀链接）
  3. 逐分类模拟点击、滚动、抽取 Top N 书籍
  4. 字体解码后增量写入 data/<slug>/snapshots/ranks_YYYYMMDD.json

兼容「无子分类」榜单（如热销/完结可能是平铺列表）：
  当页面发现不到分类目录时，退化为把当前榜单页本身当作单一伪分类抓取。

每个榜单独立的 task_state 支持中断续跑。
"""
import os
import json
import re
import time
from datetime import datetime

from playwright.sync_api import sync_playwright

from boards_config import enabled_boards

START_CODE = 58344  # 0xE3E8
CHAR_SEQUENCE = [
    "D", "在", "主", "特", "家", "军", "然", "表", "场", "4", "要", "只", "v", "和", "?", "6", "别", "还", "g", "现", "儿", "岁", "?", "?", "此", "象", "月", "3", "出", "战", "工", "相", "o", "男", "直", "失", "世", "F", "都", "平", "文", "什", "V", "O", "将", "真", "T", "那", "当", "?", "会", "立", "些", "u", "是", "十", "张", "学", "气", "大", "爱", "两", "命", "全", "后", "东", "性", "通", "被", "1", "它", "乐", "接", "而", "感", "车", "山", "公", "了", "常", "以", "何", "可", "话", "先", "p", "i", "叫", "轻", "M", "士", "w", "着", "变", "尔", "快", "l", "个", "说", "少", "色", "里", "安", "花", "远", "7", "难", "师", "放", "t", "报", "认", "面", "道", "S", "?", "克", "地", "度", "I", "好", "机", "U", "民", "写", "把", "万", "同", "水", "新", "没", "书", "电", "吃", "像", "斯", "5", "为", "y", "白", "几", "日", "教", "看", "但", "第", "加", "候", "作", "上", "拉", "住", "有", "法", "r", "事", "应", "位", "利", "你", "声", "身", "国", "问", "马", "女", "他", "Y", "比", "父", "x", "A", "H", "N", "s", "X", "边", "美", "对", "所", "金", "活", "回", "意", "到", "z", "从", "j", "知", "又", "内", "因", "点", "Q", "三", "定", "8", "R", "b", "正", "或", "夫", "向", "德", "听", "更", "?", "得", "告", "并", "本", "q", "过", "记", "L", "让", "打", "f", "人", "就", "者", "去", "原", "满", "体", "做", "经", "K", "走", "如", "孩", "c", "G", "给", "使", "物", "?", "最", "笑", "部", "?", "员", "等", "受", "k", "行", "一", "条", "果", "动", "光", "门", "头", "见", "往", "自", "解", "成", "处", "天", "能", "于", "名", "其", "发", "总", "母", "的", "死", "手", "入", "路", "进", "心", "来", "h", "时", "力", "多", "开", "已", "许", "d", "至", "由", "很", "界", "n", "小", "与", "Z", "想", "代", "么", "分", "生", "口", "再", "妈", "望", "次", "西", "风", "种", "带", "J", "?", "实", "情", "才", "这", "?", "E", "我", "神", "格", "长", "觉", "间", "年", "眼", "无", "不", "亲", "关", "结", "0", "友", "信", "下", "却", "重", "己", "老", "2", "音", "字", "m", "呢", "明", "之", "前", "高", "P", "B", "目", "太", "e", "9", "起", "稜", "她", "也", "W", "用", "方", "子", "英", "每", "理", "便", "四", "数", "期", "中", "C", "外", "样", "a", "海", "们", "任"
]


def decode_text(text: str) -> str:
    if not text:
        return ""
    result = []
    for char in text:
        code = ord(char)
        idx = code - START_CODE
        if 0 <= idx < len(CHAR_SEQUENCE):
            result.append(CHAR_SEQUENCE[idx])
        else:
            result.append(char)
    return "".join(result)


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")

# ---- 提取分类目录：榜单页内同前缀的 /rank/ 链接 ----
CATEGORIES_JS = """
() => {
    return Array.from(document.querySelectorAll('a'))
        .filter(a => /\\/rank\\/\\d+_\\d+_\\d+/.test(a.getAttribute('href') || ''))
        .map(a => ({ name: (a.innerText || '').trim(), href: a.getAttribute('href') }))
        .filter(x => x.href);
}
"""

# ---- 抽取书卡（与单榜版一致，靠 DOM 结构而非分类） ----
EXTRACT_JS = """
() => {
    const bookMap = new Map();
    const links = document.querySelectorAll('a[href^="/page/"]');
    links.forEach(link => {
        let container = link.parentElement;
        let depth = 0;
        while (container && depth < 6) {
            if (container.querySelector('img') && container.innerText.includes('在读')) {
                const href = link.getAttribute('href');
                if (!bookMap.has(href)) bookMap.set(href, container);
                break;
            }
            container = container.parentElement;
            depth++;
        }
    });
    const cards = Array.from(bookMap.values());
    const results = [];
    for (const item of cards) {
        let imgNode = item.querySelector('img');
        let cover = imgNode ? imgNode.getAttribute('src') : "";
        let title = "";
        if (imgNode && imgNode.getAttribute('alt')) title = imgNode.getAttribute('alt').trim();
        if (!title) {
            let t = item.querySelector('h4, .title, h1') || item.querySelector('a[href^="/page/"]');
            if (t) { let txt = t.innerText.trim(); if (txt && !/^\\d+$/.test(txt)) title = txt; }
        }
        if (!title) title = "未知";
        if (title.includes("榜单说明")) continue;
        let authorNode = item.querySelector('.author, .author-name') || item.querySelector('a[href^="/author-page/"]');
        let author = authorNode ? authorNode.innerText.trim() : "未知";
        let reads = "未知";
        for (let line of item.innerText.split('\\n')) {
            if (line.includes('在读')) { reads = line; break; }
        }
        let introNode = item.querySelector('.intro, .abstract, .desc');
        let intro = introNode ? introNode.innerText.trim() : "暂无简介";
        results.push({
            title, author, reads, intro, cover,
            url: item.querySelector('a[href^="/page/"]').getAttribute('href')
        });
    }
    return results;
}
"""


def clean_book(b: dict) -> dict:
    """字体解码 + 清洗单本书数据。"""
    t = decode_text(b.get("title", ""))
    a = decode_text(b.get("author", ""))
    r_raw = decode_text(b.get("reads", ""))
    i = decode_text(b.get("intro", "")).replace("\\n", " ")
    c = b.get("cover", "")
    if "在读" in r_raw:
        parts = r_raw.split("在读")
        cleaned_r = parts[1].replace(":", "").replace("：", "").strip() if len(parts) > 1 else r_raw
    else:
        cleaned_r = r_raw
    return {
        "title": t, "author": a, "reads": cleaned_r, "intro": i, "cover": c,
        "url": "https://fanqienovel.com" + b.get("url", ""),
    }


def extract_books(page, limit: int) -> list:
    """滚动加载并抽取当前页面的书卡。"""
    for _ in range(3):
        page.evaluate("window.scrollBy(0, window.innerHeight)")
        time.sleep(1.5)
    try:
        raw = page.evaluate(EXTRACT_JS)
    except Exception as e:
        print(f"    执行JS抽取失败: {e}")
        raw = []
    return [clean_book(b) for b in raw[:limit]]


def scrape_board(page, board: dict, limit: int, sleep_sec: int):
    """抓取单个榜单，写入 data/<slug>/snapshots/ranks_YYYYMMDD.json。"""
    slug = board["slug"]
    date_str = datetime.now().strftime("%Y%m%d")
    board_dir = os.path.join(DATA_DIR, slug)
    snap_dir = os.path.join(board_dir, "snapshots")
    os.makedirs(snap_dir, exist_ok=True)
    output_file = os.path.join(snap_dir, f"ranks_{date_str}.json")
    state_file = os.path.join(board_dir, f"task_state_{date_str}.json")

    # 续跑状态
    completed_cats, all_categories = [], []
    if os.path.exists(state_file):
        try:
            with open(state_file, "r", encoding="utf-8") as f:
                completed_cats = json.load(f).get("completed", [])
        except Exception:
            pass
    if os.path.exists(output_file) and completed_cats:
        try:
            with open(output_file, "r", encoding="utf-8") as f:
                all_categories = json.load(f).get("categories", [])
        except Exception:
            pass

    print(f"\n{'='*50}\n[榜单] {board['name']} ({slug}) → {board['init_url']}\n{'='*50}")
    page.goto(board["init_url"], wait_until="load", timeout=20000)
    try:
        page.wait_for_selector('a[href^="/page/"]', timeout=8000)
    except Exception:
        print("  ⚠️  未等到书籍列表，可能页面结构异常")
    time.sleep(2)

    # 发现分类
    try:
        categories = page.evaluate(CATEGORIES_JS)
    except Exception:
        categories = []
    # 去重 + 过滤空名 + 只保留本榜前缀的分类（避免页面混入其它频道/类型链接时串数据）
    rank_prefix = board.get("rank_prefix", "")
    prefix_re = re.compile(rf"/rank/{re.escape(rank_prefix)}_\d+") if rank_prefix else None
    seen, uniq_cats = set(), []
    for c in categories:
        key = c["href"]
        if key in seen or not c.get("name"):
            continue
        if prefix_re and not prefix_re.search(key):
            continue
        seen.add(key)
        uniq_cats.append(c)
    categories = uniq_cats

    if categories:
        print(f"  ✅ 发现 {len(categories)} 个分类，逐个抓取")
        _scrape_with_categories(page, categories, board, all_categories,
                                completed_cats, output_file, state_file,
                                date_str, limit, sleep_sec)
    else:
        # 无子分类 → 当前页作为单一伪分类
        print("  ℹ️  未发现分类目录，按单一榜单抓取")
        pseudo_name = board["name"]
        if pseudo_name not in completed_cats:
            books = extract_books(page, limit)
            all_categories.append({"name": pseudo_name, "books": books})
            _save_snapshot(output_file, all_categories)
            completed_cats.append(pseudo_name)
            _save_state(state_file, completed_cats)
            print(f"  成功抓取 {len(books)} 本书")

    print(f"  ✅ {board['name']} 完成，数据源：{output_file}")


def _scrape_with_categories(page, categories, board, all_categories,
                            completed_cats, output_file, state_file,
                            date_str, limit, sleep_sec):
    for cat in categories:
        cat_name, cat_href = cat["name"], cat["href"]
        if cat_name in completed_cats:
            print(f"    ⏭️  跳过已完成：{cat_name}")
            continue
        print(f"    [切换] {cat_name}")
        try:
            page.locator(f"a[href='{cat_href}']").first.click()
            time.sleep(2)
            page.wait_for_selector('a[href^="/page/"]', timeout=5000)
        except Exception as e:
            print(f"    切换/加载超时 {cat_name}: {e}")

        books = extract_books(page, limit)
        all_categories.append({"name": cat_name, "books": books})
        _save_snapshot(output_file, all_categories)
        completed_cats.append(cat_name)
        _save_state(state_file, completed_cats)
        print(f"    成功抓取 {cat_name} 前 {len(books)} 本，已存档。等待 {sleep_sec}s...")
        time.sleep(sleep_sec)


def _save_snapshot(output_file: str, all_categories: list):
    snapshot = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "categories": all_categories,
    }
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)


def _save_state(state_file: str, completed_cats: list):
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump({"completed": completed_cats}, f, ensure_ascii=False)


def run_scraper(limit=30, sleep_sec=5):
    deadline = int(os.environ.get("SCRAPE_DEADLINE_SEC", "0")) or None
    os.makedirs(DATA_DIR, exist_ok=True)
    boards = enabled_boards()
    if not boards:
        print("⚠️  没有启用的榜单。请在 boards_config.py 中配置 init_url 并设 enabled=True。")
        return

    print(f"开始抓取 {len(boards)} 个榜单：{'、'.join(b['name'] for b in boards)}")
    t0 = time.time()
    with sync_playwright() as p:
        if os.environ.get("GITHUB_ACTIONS"):
            browser = p.chromium.launch(headless=True)
        else:
            try:
                browser = p.chromium.launch(headless=True, channel="chrome")
            except Exception:
                browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36")
        )
        page = context.new_page()
        # 限制单次操作超时，避免 WAF 卡住时每个分类都干等 30s
        page.set_default_timeout(20000)

        for board in boards:
            if deadline and (time.time() - t0) > deadline:
                print(f"⏰ 已达时间预算 {deadline}s，停止后续榜单，保留已抓取数据")
                break
            try:
                scrape_board(page, board, limit, sleep_sec)
            except Exception as e:
                print(f"  ❌ 榜单 {board['name']} 抓取出错：{e}")

        browser.close()

    print("\n✅ 全部启用榜单抓取完毕。")


if __name__ == "__main__":
    print("开始执行番茄多榜单抓取计划...")
    run_scraper(limit=30, sleep_sec=5)
