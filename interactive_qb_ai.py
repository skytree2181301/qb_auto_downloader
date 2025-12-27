# -*- coding: utf-8 -*-
import feedparser
import json
import os
from qbittorrent import Client
import google.generativeai as genai
import time
import re
from json.decoder import JSONDecodeError
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, parse_qs 
import base64
from datetime import datetime, timedelta

# --- 配置及文件路径 ---
CONFIG_FILE = 'config.json'
SEEN_TORRENTS_FILE = 'seen_torrents.json'

# --- 全局变量和客户端实例 ---
CONFIG = {}
SEEN_TORRENTS = set()
QB_CLIENT = None
GEMINI_MODEL = None
CHAT_SESSION = None 
ALL_RSS_ENTRIES = [] # 存储所有解析的 RSS 条目，方便查询
LAST_SEARCH_RESULTS = [] # 存储上次搜索结果，方便用户选择下载

# --- 辅助函数：加载/保存配置和已处理的种子 ---
def load_config():
    """从 config.json 加载配置，并处理JSON解析错误"""
    global CONFIG
    if not os.path.exists(CONFIG_FILE):
        print(f"错误：配置文件 '{CONFIG_FILE}' 不存在。请根据示例创建并填写。")
        exit()
    with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
        try:
            CONFIG = json.load(f)
        except JSONDecodeError as e:
            print(f"错误：配置文件 '{CONFIG_FILE}' JSON 格式无效: {e}")
            exit()
        except Exception as e:
            print(f"错误：加载配置文件 '{CONFIG_FILE}' 时发生未知错误: {e}")
            exit()

def load_seen_torrents():
    """从 seen_torrents.json 加载已处理的种子链接, 并处理空文件或无效JSON"""
    global SEEN_TORRENTS
    if not os.path.exists(SEEN_TORRENTS_FILE):
        SEEN_TORRENTS = set()
        return

    try:
        if os.path.getsize(SEEN_TORRENTS_FILE) == 0:
             SEEN_TORRENTS = set()
             return
             
        with open(SEEN_TORRENTS_FILE, 'r', encoding='utf-8') as f:
           data = json.load(f)
           SEEN_TORRENTS = set(data) if isinstance(data, list) else set()
           
    except JSONDecodeError:
        print(f"警告: 无法解析文件 '{SEEN_TORRENTS_FILE}' (文件为空或JSON格式错误)。将返回空集合并继续。")
        SEEN_TORRENTS = set()
    except Exception as e:
         print(f"警告: 读取文件 '{SEEN_TORRENTS_FILE}' 发生错误: {e}。将返回空集合并继续。")
         SEEN_TORRENTS = set()

def save_seen_torrents():
    """将已处理的种子链接保存到 seen_torrents.json"""
    with open(SEEN_TORRENTS_FILE, 'w', encoding='utf-8') as f:
        json.dump(list(SEEN_TORRENTS), f, ensure_ascii=False, indent=4)

# --- 健壮地提取 Infohash ---
def extract_infohash(link_candidate):
    """
    从磁力链接中提取 Infohash。
    支持标准的 btih (hexadecimal) 和 btih (Base32) 格式。
    统一返回小写十六进制格式。
    """
    if not link_candidate or not link_candidate.startswith('magnet:'):
        return None
    
    parsed_uri = urlparse(link_candidate)
    query_params = parse_qs(parsed_uri.query)
    
    if 'xt' in query_params:
        for xt_param in query_params['xt']:
            if xt_param.startswith('urn:btih:'):
                infohash = xt_param[len('urn:btih:'):]
                
                if re.fullmatch(r'[0-9a-fA-F]{40,64}', infohash):
                    return infohash.lower()

                elif re.fullmatch(r'[A-Z2-7]{32,52}', infohash, re.IGNORECASE):
                    try:
                        decoded_bytes = base64.b32decode(infohash.upper(), True)
                        return decoded_bytes.hex().lower()
                    except Exception as e:
                        print(f"警告: 无法解码 Base32 infohash '{infohash}': {e}")
                        return None
    return None

# --- 从 RSS entry 或网页中提取实际下载链接 ---
def get_actual_download_link(entry):
    original_link = entry.link
    
    # 1. 优先检查 enclosure 标签
    if hasattr(entry, 'enclosures') and entry.enclosures:
        for enc in entry.enclosures:
            if enc.href:
                if enc.type == 'application/x-bittorrent' or enc.href.startswith('magnet:'):
                    return enc.href
    
    # 2. 尝试解析原始链接 (网页或直接磁力)
    if original_link and original_link.startswith('magnet:'):
        return original_link
    elif original_link and "share.dmhy.org/topics/view/" in original_link:
        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari=537.36'
            }
            response = requests.get(original_link, headers=headers, timeout=15)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, 'html.parser')

            magnet_links_on_page = soup.find_all('a', href=re.compile(r'^magnet:'))
            if magnet_links_on_page:
                return magnet_links_on_page[0]['href']
            else:
                torrent_links_on_page = soup.find_all('a', href=re.compile(r'\.torrent$'))
                if torrent_links_on_page:
                    relative_path = torrent_links_on_page[0]['href']
                    return urljoin(original_link, relative_path)
        except requests.exceptions.RequestException as req_e:
            # print(f"  访问网页 '{original_link}' 失败: {req_e}") # 减少冗余日志
            pass # 失败则静默
        except Exception as parse_e:
            # print(f"  解析网页 '{original_link}' 内容失败: {parse_e}") # 减少冗余日志
            pass # 失败则静默
    
    return original_link

# --- qBittorrent 任务添加与验证 ---
def add_and_verify_torrent(link, save_path, tags, title, unique_id):
    """
    添加 torrent 到 qBittorrent 并验证是否成功。
    返回 True if successful, False otherwise.
    """
    if CONFIG['dry_run']:
        print(f"  (模拟运行) 将下载 '{title}' 到 '{save_path}'，标签: {tags}")
        return True

    try:
        print(f"  发送下载任务到qBittorrent: {title}")
        QB_CLIENT.download_from_link(
            link,
            savepath=save_path,
            category=','.join(tags) if tags else None
        )
        
        added_successfully = False
        time.sleep(2) 

        all_torrents = QB_CLIENT.torrents()
        torrent_infohash = extract_infohash(link)

        if torrent_infohash:
            for torrent in all_torrents:
                if torrent['hash'] == torrent_infohash:
                    added_successfully = True
                    break
            
            if added_successfully:
                print(f"  任务添加成功: {title}")
            else:
                print(f"  警告: Torrent '{title}' (infohash: {torrent_infohash}) 未能在 qBittorrent 列表中找到。")
                print(f"    请手动检查 qBittorrent Web UI 或日志，确认是否添加成功或被拒绝。")
                added_successfully = False 
        else:
            print(f"  警告: 无法从链接 '{link}' 提取infohash，无法精确验证添加。")
            print(f"    请手动检查 qBittorrent Web UI，确认 '{title}' 是否被添加。")
            added_successfully = True

        return added_successfully

    except Exception as add_e:
        print(f"  添加下载任务失败 '{title}': {add_e}")
        return False

# --- Gemini AI 交互函数及工具定义 ---

# RSS 搜索工具的实现
def search_rss_items(keywords=None, quality=None, date_range=None, media_type=None, limit=10):
    """
    在所有订阅的 RSS Feed 中搜索匹配条件的资源。
    Args:
        keywords (list[str]): 搜索关键词列表，如 ["FLAC", "音乐", "OST"]。
        quality (list[str]): 质量关键词列表，如 ["FLAC", "320K", "Hi-Res"]。
        date_range (str): 日期范围，如 "this quarter", "last month", "today"。
        media_type (str): 媒体类型，如 "music", "anime", "game_music"。
        limit (int): 返回结果的最大数量。
    Returns:
        list[dict]: 匹配的资源列表，每个资源包含 "title", "original_link", "actual_download_link", "description" 等信息。
    """
    global ALL_RSS_ENTRIES, LAST_SEARCH_RESULTS

    if not ALL_RSS_ENTRIES: # 如果还没有加载 RSS，则加载
        print("\n--- 首次加载 RSS Feed（可能需要一些时间）---")
        for feed_name, feed_url in CONFIG['rss_feeds'].items():
            print(f"  正在加载 {feed_name} ({feed_url})...")
            try:
                feed = feedparser.parse(feed_url)
                if feed.bozo:
                    print(f"  警告: RSS Feed '{feed_name}' 解析错误: {feed.bozo_exception}")
                
                feed_entries_processed = 0 
                for entry in feed.entries:
                    feed_entries_processed += 1
                    if feed_entries_processed % 50 == 0:
                        print(f"    - '{feed_name}' 已处理 {feed_entries_processed} / {len(feed.entries)} 条目")

                    actual_download_link = get_actual_download_link(entry)
                    infohash = extract_infohash(actual_download_link)
                    
                    entry_unique_id = infohash if infohash else entry.link
                    
                    if entry_unique_id in SEEN_TORRENTS:
                        continue

                    ALL_RSS_ENTRIES.append({
                        "title": entry.title,
                        "original_link": entry.link,
                        "description": entry.get('description', ''),
                        "actual_download_link": actual_download_link,
                        "infohash": infohash,
                        "published_parsed": entry.get('published_parsed')
                    })
                print(f"  '{feed_name}' 加载完成，共 {len(feed.entries)} 条。")
            except Exception as e:
                print(f"  错误：加载 RSS Feed '{feed_name}' 失败: {e}")
        print(f"--- 所有 RSS Feed 加载完成，共 {len(ALL_RSS_ENTRIES)} 个有效条目可供搜索。---")

    filtered_results = []
    
    # 辅助函数：检查文本是否包含关键词（只要包含一个就匹配）
    def contains_any_keywords(text, kws):
        if not kws: return True # 如果关键词列表为空，则视为匹配
        return any(kw.lower() in text.lower() for kw in kws)

    start_date = None
    if date_range:
        today = datetime.now()
        if "quarter" in date_range.lower(): # 本季度
            current_quarter_start = datetime(today.year, 3 * ((today.month - 1) // 3) + 1, 1)
            start_date = current_quarter_start
        elif "month" in date_range.lower(): # 本月
            start_date = datetime(today.year, today.month, 1)
        elif "week" in date_range.lower(): # 本周 (假设周一为一周开始)
            start_date = today - timedelta(days=today.weekday())
        elif "today" in date_range.lower(): # 今天
            start_date = datetime(today.year, today.month, today.day)
        # 可以添加更多日期范围逻辑，或让AI返回精确日期

    for entry_data in ALL_RSS_ENTRIES: 
        text_to_search = entry_data["title"] + " " + entry_data["description"]
        
        # 关键词过滤（使用 any_keywords）
        if keywords and not contains_any_keywords(text_to_search, keywords):
            continue
        if quality and not contains_any_keywords(text_to_search, quality):
            continue
        
        # 媒体类型过滤
        if media_type:
            media_type_lower = media_type.lower()
            type_keywords = []
            if media_type_lower == "music":
                type_keywords = ["音乐", "song", "album", "single", "ost", "vgm", "原声"]
            elif media_type_lower == "anime":
                type_keywords = ["动漫", "动画", "anime", "番剧", "剧场版"]
            elif media_type_lower == "game_music":
                type_keywords = ["游戏音乐", "game music", "vgm"]
            # 可以添加更多类型及其关键词
            if not type_keywords or not contains_any_keywords(text_to_search, type_keywords):
                continue


        if start_date and entry_data.get('published_parsed'):
            entry_date = datetime(*entry_data['published_parsed'][:6])
            if entry_date < start_date:
                continue

        filtered_results.append(entry_data)
        if len(filtered_results) >= limit:
            break
            
    LAST_SEARCH_RESULTS = filtered_results 
    return [
        {
            "index": i + 1,
            "title": res["title"],
            "original_link": res["original_link"] 
        } for i, res in enumerate(filtered_results)
    ]

# 定义 Gemini 可以使用的工具
TOOL_FUNCTIONS = [
    search_rss_items
]


# --- 主逻辑函数 ---
def main():
    global QB_CLIENT, GEMINI_MODEL, CHAT_SESSION, LAST_SEARCH_RESULTS

    load_config()
    load_seen_torrents()
    
    qb_config = CONFIG['qbittorrent']
    gemini_config = CONFIG['gemini']

    print(f"脚本以 {'模拟运行模式' if CONFIG['dry_run'] else '实际运行模式'} 启动。")

    # 连接 qBittorrent 客户端
    try:
        QB_CLIENT = Client(qb_config['url'])
        QB_CLIENT.login(qb_config['username'], qb_config['password'])
        print(f"成功连接到 qBittorrent ({qb_config['url']}).")
    except Exception as e:
        print(f"连接或登录 qBittorrent 失败: {e}")
        print("请检查 qBittorrent Web UI 是否开启，以及配置文件中的 URL、用户名和密码是否正确。")
        exit()

    # 初始化 Gemini 模型和对话会话
    try:
        genai.configure(api_key=gemini_config['api_key'])
        
        GEMINI_MODEL = genai.GenerativeModel(
            model_name=gemini_config['model_name'],
            tools=TOOL_FUNCTIONS
        )
        
        CHAT_SESSION = GEMINI_MODEL.start_chat(history=[]) 
        print("\nAI 助手已启动，请开始提问！(输入 'exit' 退出, 'download #<num>' 下载)")
    except Exception as e:
        print(f"初始化 Gemini AI 失败: {e}")
        print("请检查配置文件中的 Gemini API Key 和模型名称。")
        exit()

    # --- 对话循环 ---
    while True:
        try:
            user_input = input("\n你: ")
            if user_input.lower() == 'exit':
                print("AI: 再见！")
                break

            # 处理用户下载指令
            if user_input.lower().startswith('download #'):
                try:
                    # 先移除 "download " 前缀
                    temp_str = user_input.lower().replace('download ', '').strip()
                    # 移除所有 # 符号
                    temp_str = temp_str.replace('#', '')
                    
                    # 按逗号分割，并过滤掉空字符串和非数字部分
                    selected_indices = []
                    for s in temp_str.split(','):
                        s_stripped = s.strip()
                        if s_stripped.isdigit():
                            selected_indices.append(int(s_stripped))
                    
                    if not selected_indices: # 如果解析不到任何有效数字
                        print("AI: 无效的下载指令格式。请使用 'download #<序号>' 或 'download <序号1>,<序号2>'。")
                        continue # 跳过当前循环，重新等待输入
                    
                    if not LAST_SEARCH_RESULTS:
                        print("AI: 请先进行搜索，然后选择要下载的资源。")
                        continue
                    
                    for idx in selected_indices:
                        if 1 <= idx <= len(LAST_SEARCH_RESULTS):
                            selected_res = LAST_SEARCH_RESULTS[idx - 1]
                            title = selected_res["title"]
                            actual_download_link = selected_res["actual_download_link"]
                            unique_id = selected_res["infohash"] if selected_res["infohash"] else selected_res["original_link"]

                            if unique_id in SEEN_TORRENTS:
                                print(f"  资源 '{title}' 已在已处理列表中，跳过下载。")
                                continue

                            default_path = CONFIG.get('default_download_path', '/downloads/Others')
                            default_tags = ['自动下载'] 

                            print(f"\nAI: 准备下载 '{title}'...")
                            if add_and_verify_torrent(actual_download_link, default_path, default_tags, title, unique_id):
                                SEEN_TORRENTS.add(unique_id) 
                                save_seen_torrents()
                            else:
                                print(f"AI: 下载 '{title}' 失败。请检查日志或手动下载。")
                        else:
                            print(f"AI: 序号 #{idx} 无效。请选择列表中的有效序号。")
                except ValueError:
                    print("AI: 无效的下载指令格式。请使用 'download #<序号>' 或 'download #<序号1>, #<序号2>'。")
                except Exception as e:
                    print(f"AI: 处理下载指令时发生错误: {e}")
                continue 

            # 将用户输入发送给 Gemini
            # 彻底移除 safety_settings 参数，使用 Gemini 默认安全策略
            response = CHAT_SESSION.send_message(user_input)

            # 处理 Gemini 的响应：健壮地遍历所有 parts
            printed_response = False
            for part in response.parts:
                if part.function_call:
                    tool_call = part.function_call
                    function_name = tool_call.name
                    function_args = tool_call.args

                    if function_name == "search_rss_items":
                        print(f"AI: 正在执行搜索任务...")
                        
                        args_dict = function_args._asdict() if hasattr(function_args, '_asdict') else dict(function_args)
                        results = search_rss_items(**args_dict)
                        
                        tool_response_part = genai.protos.Part(
                            function_response=genai.protos.FunctionResponse(
                                name="search_rss_items",
                                response={"results": results}
                            )
                        )
                        # 将工具执行结果发送回 Gemini，并获取最终的文本响应
                        # 这里也不再传入 safety_settings
                        final_response_from_tool = CHAT_SESSION.send_message(tool_response_part)
                        
                        for final_part in final_response_from_tool.parts:
                            if final_part.text:
                                print("AI:", final_part.text)
                                printed_response = True
                                break 
                        
                        if not printed_response:
                            print("AI: 抱歉，搜索结果已返回，但我无法以文本形式呈现。")

                    else:
                        print(f"AI: 我不明白你想做什么，或者我没有执行 '{function_name}' 的工具。")
                elif part.text:
                    print("AI:", part.text)
                    printed_response = True
            
            if not printed_response:
                print("AI: 抱歉，我收到一个非文本或无法解析的响应。")

        except KeyboardInterrupt:
            print("\nAI: 收到中断信号，退出对话。")
            break
        except Exception as e:
            print(f"AI: 发生未知错误: {e}")
            print("AI: 请尝试重新开始对话。")

    if QB_CLIENT:
        try:
            pass 
        except Exception as e:
            print(f"退出 qBittorrent 登录时发生错误: {e}")

    print("\n脚本执行完毕。")

if __name__ == "__main__":
    main()