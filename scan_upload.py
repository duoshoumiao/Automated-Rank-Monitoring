# -*- coding: utf-8 -*-  
"""  
独立的「自动扫描公会排名 + 上传 GitHub」脚本。  
放在仓库根目录（与 pcrclient.py / bsgamesdk.py / rsacr.py 同级），  
直接运行： python scan_upload.py  
  
依赖： pip install httpx requests pycryptodome msgpack python-dateutil loguru  
自动过码失败时会回退为控制台手动过码（按提示在浏览器完成验证后粘贴 validate）
"""  
  
import os  
import sys  
import json  
import time  
import base64  
import types  
import asyncio  
import logging  
import importlib.util  
  
# ============================================================  
# 1. 用户配置区（改成你自己的）  
# ============================================================  
  
# 账号类型： 0 = B站账号密码服   1 = 渠道服(login_id + token)  
QUDAO = 0  
  
# --- QUDAO = 0 时填这两个 ---  
ACCOUNT = "aea89e"  
PASSWORD = "Aa856567"  
  
# --- QUDAO = 1 时填这两个 ---  
UID = "你的login_id"  
ACCESS_KEY = "你的token令牌"  
  
# --- GitHub 配置 ---  
GITHUB_PAT = ""          # 个人访问令牌  
GITHUB_REPO = "duoshoumiao/chagonghui"  
GITHUB_FILE_PATH = "clan_scan/clan_ranking_global.json"  
GITHUB_BRANCH = "main"  
  
# 本地保存路径  
SCAN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "clan_ranking_global.json")  
  
# 扫描上限与节流  
MAX_CLANS = 3000  
PAGE_SLEEP = 0.3          # 每页之间的间隔（秒）  
MAX_UPLOAD_RETRIES = 5  
  
# ============================================================  
# 2. 日志  
# ============================================================  
logging.basicConfig(level=logging.INFO,  
                    format="%(asctime)s [%(levelname)s] %(message)s")  
logger = logging.getLogger("scan_upload")  
  
# ============================================================  
# 3. 复用仓库内的 pcrclient / bsgamesdk / rsacr  
#    （通过构造一个临时包，满足它们内部的相对导入 + 屏蔽 nonebot 依赖）  
# ============================================================  
REPO_DIR = os.path.dirname(os.path.abspath(__file__))  
PKG_NAME = "_pcr_standalone_pkg"  
  
# 屏蔽 nonebot（pcrclient.py 里 `from nonebot import logger`）  
_nonebot_stub = types.ModuleType("nonebot")  
_nonebot_stub.logger = logger  
sys.modules.setdefault("nonebot", _nonebot_stub)  
  
# 注册一个假的包，让相对导入 `from . import rsacr` 生效  
_pkg = types.ModuleType(PKG_NAME)  
_pkg.__path__ = [REPO_DIR]  
sys.modules[PKG_NAME] = _pkg  
  
  
def _load_module(name):  
    spec = importlib.util.spec_from_file_location(  
        f"{PKG_NAME}.{name}", os.path.join(REPO_DIR, f"{name}.py"))  
    mod = importlib.util.module_from_spec(spec)  
    sys.modules[f"{PKG_NAME}.{name}"] = mod  
    spec.loader.exec_module(mod)  
    return mod  
  
  
_load_module("rsacr")          # bsgamesdk 内部 `from . import rsacr`  
_load_module("bsgamesdk")      # pcrclient 内部 `from .bsgamesdk import bsdkclient`  
_pcrclient_mod = _load_module("pcrclient")  
  
pcrclient = _pcrclient_mod.pcrclient  
bsdkclient = _pcrclient_mod.bsdkclient  
  
import requests  # noqa: E402  
  
  
# ============================================================  
# 4. 自动过码（复用 login.py 里的逻辑，简化内联）  
# ============================================================  
import httpx  # noqa: E402  
  
_captcha_header = {"Content-Type": "application/json",  
                   "User-Agent": "pcrjjc2/1.0.0"}  
  
  
async def captchaVerifier_auto(*args):  
    gt, challenge, userid = args[0], args[1], args[2]  
    async with httpx.AsyncClient(timeout=30) as ac:  
        res = (await ac.get(  
            f"https://pcrd.tencentbot.top/geetest_renew?captcha_type=1"  
            f"&challenge={challenge}&gt={gt}&userid={userid}&gs=1",  
            headers=_captcha_header)).json()  
        uuid = res["uuid"]  
        ccnt = 0  
        while (ccnt := ccnt + 1) < 10:  
            res = (await ac.get(  
                f"https://pcrd.tencentbot.top/check/{uuid}",  
                headers=_captcha_header)).json()  
            if "queue_num" in res:  
                await asyncio.sleep(min(int(res["queue_num"]), 3) * 10)  
                continue  
            info = res["info"]  
            if isinstance(info, dict) and "validate" in info:  
                return info["challenge"], info["gt_user_id"], info["validate"]  
            if info in ("fail", "url invalid"):  
                raise Exception("自动过码失败")  
            await asyncio.sleep(5)  
        raise Exception("自动过码多次失败")  

MANUAL_CAPTCHA_TIMEOUT = 180   # 手动过码等待超时（秒）  
  
  
async def manual_captcha_console(gt, challenge, userid):  
    """自动过码失败时，控制台手动过码。"""  
    url = (f"https://help.tencentbot.top/geetest/?captcha_type=1"  
           f"&challenge={challenge}&gt={gt}&userid={userid}&gs=1")  
    print("\n" + "=" * 60)  
    print("自动过码失败，请手动完成验证：")  
    print(f"1. 在浏览器打开： {url}")  
    print("2. 完成验证后，复制第一个方框中的内容（validate）粘贴到此处")  
    print("=" * 60)  
    loop = asyncio.get_event_loop()  
    try:  
        validate = await asyncio.wait_for(  
            loop.run_in_executor(None, input, "请输入validate: "),  
            timeout=MANUAL_CAPTCHA_TIMEOUT)  
    except asyncio.TimeoutError:  
        raise Exception("手动过码超时，取消登录")  
    validate = validate.strip()  
    if not validate:  
        raise Exception("未输入validate，手动过码取消")  
    return challenge, userid, validate  
  
  
async def captchaVerifier(*args):  
    """先自动过码，失败则回退到控制台手动过码。"""  
    gt, challenge, userid = args[0], args[1], args[2]  
    try:  
        return await asyncio.wait_for(  
            captchaVerifier_auto(*args), timeout=120)  
    except Exception as e:  
        logger.warning(f"自动过码失败({e})，转为手动过码")  
        return await manual_captcha_console(gt, challenge, userid)  
  
# ============================================================  
# 5. 工具：找会战币  
# ============================================================  
def find_item(item_list, item_id):  
    for item in item_list:  
        if item["id"] == item_id:  
            return item["stock"]  
    return 0  
  
  
# ============================================================  
# 6. 上传 GitHub  
# ============================================================  
GITHUB_API_BASE = (  
    f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_FILE_PATH}")  
  
  
def _get_github_file_sha():  
    headers = {"Accept": "application/vnd.github.v3+json"}  
    if GITHUB_PAT:  
        headers["Authorization"] = f"token {GITHUB_PAT}"  
    resp = requests.get(GITHUB_API_BASE, headers=headers)  
    if resp.status_code == 200:  
        return resp.json().get("sha")  
    return None  
  
  
def _upload_scan_to_github():  
    if not GITHUB_PAT or GITHUB_PAT.startswith("你的"):  
        logger.warning("[自动扫描] 未配置 GITHUB_PAT，跳过上传")  
        return False  
    if not os.path.exists(SCAN_FILE):  
        logger.warning("[自动扫描] 本地数据文件不存在，跳过上传")  
        return False  
    with open(SCAN_FILE, "r", encoding="utf-8") as f:  
        content = f.read()  
    content_b64 = base64.b64encode(content.encode("utf-8")).decode("utf-8")  
    headers = {  
        "Authorization": f"token {GITHUB_PAT}",  
        "Accept": "application/vnd.github.v3+json",  
    }  
    payload = {  
        "message": "自动更新公会排名数据",  
        "content": content_b64,  
        "branch": GITHUB_BRANCH,  
    }  
    for attempt in range(1, MAX_UPLOAD_RETRIES + 1):  
        try:  
            sha = _get_github_file_sha()  
            if sha:  
                payload["sha"] = sha  
            resp = requests.put(GITHUB_API_BASE, json=payload, headers=headers)  
            if resp.status_code in (200, 201):  
                logger.info(f"[自动扫描] 已成功上传到 GitHub（第 {attempt} 次尝试）")  
                return True  
            if resp.status_code in (401, 403):  
                logger.error(f"[自动扫描] 鉴权失败 HTTP {resp.status_code}，放弃上传")  
                return False  
            logger.error(f"[自动扫描] 第 {attempt} 次上传失败: HTTP {resp.status_code} "  
                         f"{resp.json().get('message', '')}")  
        except Exception as e:  
            logger.exception(f"[自动扫描] 第 {attempt} 次上传异常: {e}")  
        if attempt < MAX_UPLOAD_RETRIES:  
            time.sleep(min(2 ** attempt, 30))  
    logger.error(f"[自动扫描] 已重试 {MAX_UPLOAD_RETRIES} 次仍失败，放弃")  
    return False  
  
  
# ============================================================  
# 7. 登录 + 取 clan_id / clan_battle_id  
# ============================================================  
async def build_client():  
    if QUDAO == 0:  
        acc = {"platform": 2, "channel": 1,  
               "account": ACCOUNT, "password": PASSWORD}  
    else:  
        acc = {"platform": 4, "channel": 1, "qudao": 1,  
               "uid": UID, "access_key": ACCESS_KEY}  
    client = pcrclient(bsdkclient(acc, captchaVerifier))  
    await client.login()  
    logger.info("登录成功")  
    return client  
  
  
async def get_clan_ids(client):  
    home = await client.callapi("/home/index", {  
        "message_id": 1, "tips_id_list": [], "is_first": 1, "gold_history": 0})  
    if not home.get("user_clan"):  
        raise Exception("该账号今天还没登录过游戏 / 未加入公会，请先进游戏后再试")  
    clan_id = home["user_clan"]["clan_id"]  
  
    load_index = await client.callapi("/load/index", {"carrier": "OPPO"})  
    coin = find_item(load_index["item_list"], 90006)  
  
    top = await client.callapi("/clan_battle/top", {  
        "clan_id": clan_id, "is_first": 0, "current_clan_battle_coin": coin})  
    clan_battle_id = top["clan_battle_id"]  
    logger.info(f"clan_id={clan_id} clan_battle_id={clan_battle_id}")  
    return clan_id, clan_battle_id  
  
  
# ============================================================  
# 8. 扫描排名  
# ============================================================  
async def scan_ranking(client, clan_id, clan_battle_id):  
    all_clans = {}  
    failed_pages = []  
  
    async def fetch(page):  
        return await client.callapi("/clan_battle/period_ranking", {  
            "clan_id": clan_id,  
            "clan_battle_id": clan_battle_id,  
            "period": 1, "month": 0, "page": page,  
            "is_my_clan": 0, "is_first": 1,  
        })  
  
    def collect(page_info):  
        for rank in page_info["period_ranking"]:  
            if len(all_clans) >= MAX_CLANS:  
                break  
            rank_num = rank.get("rank", 0)  
            all_clans[str(rank_num)] = {  
                "rank": rank_num,  
                "clan_name": rank.get("clan_name", "该公会可能已解散"),  
                "leader_name": rank.get("leader_name", "未知"),  
                "damage": rank.get("damage", 0),  
                "member_num": rank.get("member_num", 0),  
                "grade_rank": rank.get("grade_rank", 0),  
            }  
  
    for page in range(300):  
        if len(all_clans) >= MAX_CLANS:  
            logger.info(f"[自动扫描] 已达 {MAX_CLANS} 个公会上限，停止")  
            break  
        try:  
            page_info = await fetch(page)  
            ranking = page_info.get("period_ranking")  
            if not ranking:          # None（没有该字段）或 [] 都表示到底了  
                logger.info(f"[自动扫描] 第{page}页无数据，排名已到底，停止翻页")  
                break  
            collect({"period_ranking": ranking})  
            await asyncio.sleep(PAGE_SLEEP)  
        except Exception as e:  
            logger.error(f"[自动扫描] 第{page}页失败: {e}")  
            await asyncio.sleep(2)  
            try:  
                page_info = await fetch(page)  
                ranking = page_info.get("period_ranking")  
                if not ranking:  
                    logger.info(f"[自动扫描] 第{page}页重试仍无数据，停止翻页")  
                    break  
                collect({"period_ranking": ranking})  
            except Exception as e2:  
                logger.error(f"[自动扫描] 重试第{page}页仍失败: {e2}")  
                failed_pages.append(page)  
            await asyncio.sleep(1)  
            continue
  
    return all_clans, failed_pages  
  
  
# ============================================================  
# 9. 主流程  
# ============================================================  
async def main():  
    client = await build_client()  
    clan_id, clan_battle_id = await get_clan_ids(client)  
  
    all_clans, failed_pages = await scan_ranking(client, clan_id, clan_battle_id)  
    if not all_clans:  
        logger.warning("[自动扫描] 未获取到任何公会数据")  
        return  
  
    with open(SCAN_FILE, "w", encoding="utf-8") as f:  
        json.dump(all_clans, f, ensure_ascii=False, indent=2)  
    logger.info(f"[自动扫描] 扫描完成，共 {len(all_clans)} 个公会"  
                + (f"，失败页: {len(failed_pages)}" if failed_pages else ""))  
  
    _upload_scan_to_github()  
  
  
# 每小时的 00:01 和 30:01 各扫描一次（对齐库里 minute='1,31' 的思路，精确到秒）  
RUN_SECOND = 1          # 每个触发点的秒数：第 1 秒  
RUN_MINUTES = (0, 30)   # 每小时在第 0 分和第 30 分触发
  
def _seconds_until_next_run():  
    """计算距离下一个触发点（每小时 00:01 / 30:01）还有多少秒。"""  
    now = time.localtime()  
    now_ts = time.time()  
    # 本小时内的候选触发点：mm:RUN_SECOND  
    candidates = []  
    for m in RUN_MINUTES:  
        t = list(now)  
        t[4] = m            # 分  
        t[5] = RUN_SECOND   # 秒  
        candidates.append(time.mktime(time.struct_time(tuple(t))))  
    # 再加上下一个小时的第一个触发点，防止本小时的都已过去  
    t = list(now)  
    t[3] = now.tm_hour + 1  # 下一小时（mktime 会自动归一化跨天）  
    t[4] = RUN_MINUTES[0]  
    t[5] = RUN_SECOND  
    candidates.append(time.mktime(time.struct_time(tuple(t))))  
    # 取第一个还没到的触发点  
    future = [c for c in candidates if c > now_ts]  
    return max(0, min(future) - now_ts)  
  
  
async def loop_main():  
    while True:  
        wait = _seconds_until_next_run()  
        next_at = time.strftime("%H:%M:%S", time.localtime(time.time() + wait))  
        logger.info(f"[自动扫描] 下次扫描时间 {next_at}（{int(wait)} 秒后）")  
        await asyncio.sleep(wait)  
        try:  
            await main()  
        except Exception as e:  
            logger.exception(f"[自动扫描] 本轮执行异常: {e}")  
        # 睡 2 秒避免本轮结束得太快、在同一触发点重复触发  
        await asyncio.sleep(2)  
  
  
if __name__ == "__main__":  
    asyncio.run(loop_main())